import base64
import hashlib
import logging
import os
import re
import time
import asyncio
from datetime import datetime
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from aiohttp import ClientTimeout
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import config
from src.models import DVR, SetupResult, SetupStatus
from src.xml_builder import XMLBuilder

logger = logging.getLogger(__name__)


def _parse_digest_challenge(header_val: str) -> dict:
    if not header_val:
        return {}
    token_str = header_val
    if token_str.startswith("Digest "):
        token_str = token_str[7:]
    elif token_str.startswith("Basic "):
        return {"scheme": "basic"}

    pattern = r'(\w+)=(?:"([^"]*)"|([^\s,]+))'
    matches = re.findall(pattern, token_str)
    res = {}
    for k, v1, v2 in matches:
        res[k] = v1 if v1 else v2
    return res


def _md5_hex(data: str) -> str:
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def _build_digest_header(
    method: str, uri: str, username: str, password: str, challenge: dict, nc_count: int = 1
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "")
    opaque = challenge.get("opaque")

    ha1 = _md5_hex(f"{username}:{realm}:{password}")
    ha2 = _md5_hex(f"{method}:{uri}")

    nc = f"{nc_count:08x}"
    cnonce = os.urandom(8).hex()

    if qop and "auth" in qop.split(","):
        resp = _md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        auth_header = (
            f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}", qop=auth, nc={nc}, cnonce="{cnonce}"'
        )
    else:
        resp = _md5_hex(f"{ha1}:{nonce}:{ha2}")
        auth_header = (
            f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}"'
        )

    if opaque:
        auth_header += f', opaque="{opaque}"'
    return auth_header


class DVRClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.timeout = ClientTimeout(total=config.get("execution", "request_timeout", default=10))
        self.test_timeout = ClientTimeout(total=config.get("execution", "test_email_timeout", default=45))
        self._auth_cache: Dict[Tuple[str, int], dict] = {}
        self._nc_cache: Dict[Tuple[str, int], int] = {}

    async def _request(
        self,
        method: str,
        url: str,
        dvr: DVR,
        data: Optional[str] = None,
        headers: Optional[dict] = None,
        timeout: Optional[ClientTimeout] = None,
    ) -> Tuple[int, str]:
        req_headers = dict(headers) if headers else {}
        timeout = timeout or self.timeout
        dvr_key = (dvr.ip, dvr.port)

        parsed = urlparse(url)
        uri = parsed.path
        if parsed.query:
            uri += f"?{parsed.query}"

        # If we have a cached challenge, attempt pre-emptive digest auth
        cached_challenge = self._auth_cache.get(dvr_key)
        if cached_challenge:
            if cached_challenge.get("scheme") == "basic":
                token = base64.b64encode(f"{dvr.username}:{dvr.password}".encode()).decode()
                req_headers["Authorization"] = f"Basic {token}"
            else:
                nc = self._nc_cache.get(dvr_key, 1) + 1
                self._nc_cache[dvr_key] = nc
                req_headers["Authorization"] = _build_digest_header(
                    method, uri, dvr.username, dvr.password, cached_challenge, nc_count=nc
                )

        async with self.session.request(method, url, data=data, headers=req_headers, timeout=timeout) as resp:
            if resp.status != 401:
                return resp.status, await resp.text()

            # Handle 401 Challenge
            www_auth = resp.headers.get("WWW-Authenticate", "")
            if "Digest" in www_auth:
                challenge = _parse_digest_challenge(www_auth)
                self._auth_cache[dvr_key] = challenge
                self._nc_cache[dvr_key] = 1
                req_headers["Authorization"] = _build_digest_header(
                    method, uri, dvr.username, dvr.password, challenge, nc_count=1
                )
            elif "Basic" in www_auth:
                self._auth_cache[dvr_key] = {"scheme": "basic"}
                token = base64.b64encode(f"{dvr.username}:{dvr.password}".encode()).decode()
                req_headers["Authorization"] = f"Basic {token}"
            else:
                return resp.status, await resp.text()

        # Retry with computed authorization
        async with self.session.request(method, url, data=data, headers=req_headers, timeout=timeout) as resp2:
            return resp2.status, await resp2.text()

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type(aiohttp.ClientError),
        reraise=True,
    )
    async def get_model(self, dvr: DVR) -> str:
        url = f"http://{dvr.ip}:{dvr.port}/ISAPI/System/deviceInfo"
        try:
            status, text = await self._request("GET", url, dvr, timeout=self.timeout)
            if status == 200:
                m = re.search(r"<model>(.*?)</model>", text, re.IGNORECASE)
                return m.group(1).strip() if m else "Unknown"
        except Exception as e:
            logger.debug(f"Model fetch failed [{dvr.ip}]: {e}")
        return "Unknown"

    async def save_email_config(self, dvr: DVR) -> Tuple[bool, str]:
        headers = {"Content-Type": "application/xml"}
        xml = XMLBuilder.email_config(dvr.ip)
        urls = [
            f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Network/mailing/1",
            f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Network/mailing",
        ]
        for url in urls:
            try:
                status, body = await self._request(
                    "PUT", url, dvr, data=xml, headers=headers, timeout=self.timeout
                )
                if status in (200, 204):
                    return True, "Configuration saved"
                if status != 404:
                    err_m = re.search(r"<(?:statusString|subStatusString|errorDescription)>(.*?)</", body, re.IGNORECASE)
                    err_info = err_m.group(1).strip() if err_m else body[:80].replace("\n", " ")
                    return False, f"Save failed (DVR returned HTTP {status}: {err_info})"
            except aiohttp.ClientError as e:
                return False, f"Save network error: {str(e)[:80]}"
        return False, "Endpoint not found (404)"

    async def set_dvr_dns(self, dvr: DVR) -> None:
        """
        Automatic DNS Fix: Ensures DVR has Google DNS (8.8.8.8).
        Isse 'Test failed: failed' (smtp.gmail resolve na hone ka error) fix hota hai.
        """
        url = f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Network/interfaces/1"
        try:
            st, xml = await self._request("GET", url, dvr)
            if st != 200:
                return

            modified = False
            if "<primaryDNS>" in xml:
                if "<primaryDNS>8.8.8.8</primaryDNS>" not in xml:
                    xml = re.sub(r'<primaryDNS>.*?</primaryDNS>', '<primaryDNS>8.8.8.8</primaryDNS>', xml)
                    modified = True
            else:
                xml = xml.replace('</IPAddress>', '</IPAddress>\n<primaryDNS>8.8.8.8</primaryDNS>')
                modified = True

            if modified:
                put_st, _ = await self._request(
                    "PUT", url, dvr, data=xml, headers={"Content-Type": "application/xml"}, timeout=self.timeout
                )
                if put_st in (200, 204):
                    await asyncio.sleep(1.5)
                    logger.debug(f"[{dvr.ip}] DNS updated to 8.8.8.8")
        except Exception as e:
            logger.debug(f"[{dvr.ip}] Failed to set DNS: {e}")

    async def send_test_email(self, dvr: DVR) -> Tuple[bool, str]:
        url = f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Network/mailing/test"
        xml = XMLBuilder.test_email(dvr.ip)
        headers = {"Content-Type": "application/xml"}
        try:
            status, text = await self._request(
                "POST", url, dvr, data=xml, headers=headers, timeout=self.test_timeout
            )
            if status in (200, 204) and "<errorDescription>ok</errorDescription>" in text:
                return True, "Test email sent"

            # Extract detailed error from DVR response
            error_desc = re.search(r"<errorDescription>(.*?)</errorDescription>", text, re.IGNORECASE)
            status_str = re.search(r"<statusString>(.*?)</statusString>", text, re.IGNORECASE)
            sub_status = re.search(r"<subStatusString>(.*?)</subStatusString>", text, re.IGNORECASE)

            reasons = []
            if error_desc:
                reasons.append(f"errorDescription='{error_desc.group(1).strip()}'")
            if status_str:
                reasons.append(f"statusString='{status_str.group(1).strip()}'")
            if sub_status:
                reasons.append(f"subStatus='{sub_status.group(1).strip()}'")

            if reasons:
                err_detail = ", ".join(reasons)
            else:
                clean_text = text[:100].replace("\n", " ").strip()
                err_detail = clean_text or "No response body"

            return False, f"DVR Error [HTTP {status}: {err_detail}]"
        except asyncio.TimeoutError:
            timeout_sec = int(self.test_timeout.total) if self.test_timeout and self.test_timeout.total else 45
            return False, f"DVR Error [Timeout after {timeout_sec}s: DVR cannot reach SMTP server/DNS]"
        except aiohttp.ClientError as e:
            return False, f"DVR Error [Network error: {str(e)[:60]}]"
        except Exception as e:
            return False, f"DVR Error [Exception: {str(e)[:60]}]"


    async def enable_channel_detections(self, dvr: DVR):
        """Ensure Motion Detection, Video Loss, and Tamper Detection are enabled on all channels."""
        try:
            st, ch_text = await self._request("GET", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels", dvr)
            if st != 200:
                return
            ch_ids = re.findall(r"<id>(\d+)</id>", ch_text)
            headers = {"Content-Type": "application/xml"}

            for ch in ch_ids:
                # 1. Motion Detection
                st_m, m_xml = await self._request("GET", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels/{ch}/motionDetection", dvr)
                if st_m == 200 and "<enabled>false</enabled>" in m_xml:
                    new_m = m_xml.replace("<enabled>false</enabled>", "<enabled>true</enabled>")
                    await self._request("PUT", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels/{ch}/motionDetection", dvr, data=new_m, headers=headers)

                # 2. Video Loss
                st_v, v_xml = await self._request("GET", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels/{ch}/videoLoss", dvr)
                if st_v == 200 and "<enabled>false</enabled>" in v_xml:
                    new_v = v_xml.replace("<enabled>false</enabled>", "<enabled>true</enabled>")
                    await self._request("PUT", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels/{ch}/videoLoss", dvr, data=new_v, headers=headers)

                # 3. Tamper Detection
                st_t, t_xml = await self._request("GET", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels/{ch}/tamperDetection", dvr)
                if st_t == 200 and "<enabled>false</enabled>" in t_xml:
                    new_t = t_xml.replace("<enabled>false</enabled>", "<enabled>true</enabled>")
                    await self._request("PUT", f"http://{dvr.ip}:{dvr.port}/ISAPI/System/Video/inputs/channels/{ch}/tamperDetection", dvr, data=new_t, headers=headers)
        except Exception as e:
            logger.debug(f"Channel detection enable error [{dvr.ip}]: {e}")

    async def enable_event_email_linkage(self, dvr: DVR) -> Tuple[bool, str]:
        events_conf = config.get("events") or {}
        if not events_conf.get("enable_email_linkage", True):
            return True, "Event linkage disabled in config"

        try:
            # First ensure detection is enabled on all channels
            await self.enable_channel_detections(dvr)

            st, text = await self._request("GET", f"http://{dvr.ip}:{dvr.port}/ISAPI/Event/triggers", dvr)
            if st != 200:
                return False, f"Could not fetch event triggers (HTTP {st})"

            triggers = re.findall(r"<EventTrigger\b[^>]*>(.*?)</EventTrigger>", text, re.DOTALL)
            updated_count = 0
            already_count = 0

            email_elem = "<EventTriggerNotification><id>email</id><notificationMethod>email</notificationMethod></EventTriggerNotification>"

            for trig in triggers:
                m_id = re.search(r"<id>(.*?)</id>", trig)
                if not m_id:
                    continue
                t_id = m_id.group(1).strip()

                if "<notificationMethod>email</notificationMethod>" in trig:
                    already_count += 1
                    continue

                t_st, t_xml = await self._request("GET", f"http://{dvr.ip}:{dvr.port}/ISAPI/Event/triggers/{t_id}", dvr)
                if t_st != 200:
                    continue

                if "</EventTriggerNotificationList>" in t_xml:
                    new_xml = t_xml.replace("</EventTriggerNotificationList>", f"{email_elem}</EventTriggerNotificationList>")
                else:
                    new_xml = t_xml.replace("</EventTrigger>", f"<EventTriggerNotificationList>{email_elem}</EventTriggerNotificationList></EventTrigger>")

                put_st, _ = await self._request(
                    "PUT",
                    f"http://{dvr.ip}:{dvr.port}/ISAPI/Event/triggers/{t_id}",
                    dvr,
                    data=new_xml,
                    headers={"Content-Type": "application/xml"},
                    timeout=self.timeout,
                )
                if put_st in (200, 204):
                    updated_count += 1

            total_linked = updated_count + already_count
            return True, f"Event email linked ({total_linked} triggers)"
        except Exception as e:
            return False, f"Event linkage error: {str(e)[:60]}"

    @staticmethod
    async def is_reachable(ip: str, port: int, timeout: float = None) -> bool:
        """Fast TCP socket ping to check if DVR is online and port is open (<1.5s)."""
        if timeout is None:
            timeout = config.get("execution", "ping_timeout", default=1.5)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def configure(self, dvr: DVR) -> SetupResult:
        start = time.time()

        # 0. Fast Pre-Check / Socket Ping (<1.5s)
        online = await self.is_reachable(dvr.ip, dvr.port)
        if not online:
            return SetupResult(
                atmid=dvr.atmid,
                ip=dvr.ip,
                port=dvr.port,
                model="Unknown",
                status=SetupStatus.FAILED,
                remark=f"DVR Unreachable (Offline / Port {dvr.port} Closed)",
                duration_ms=int((time.time() - start) * 1000),
                timestamp=datetime.now(),
            )

        # 1. Fetch Model Name
        model = await self.get_model(dvr)

        # 2. Save Email Configuration
        saved, save_msg = await self.save_email_config(dvr)
        if not saved:
            return SetupResult(
                atmid=dvr.atmid,
                ip=dvr.ip,
                port=dvr.port,
                model=model,
                status=SetupStatus.FAILED,
                remark=save_msg,
                duration_ms=int((time.time() - start) * 1000),
                timestamp=datetime.now(),
            )

        # 3. Ensure DVR has Google DNS before testing Email
        await self.set_dvr_dns(dvr)

        # 4. Enable Events & Link to Email
        events_linked, events_msg = await self.enable_event_email_linkage(dvr)
        
        # 5. Send Test Email
        tested, test_msg = await self.send_test_email(dvr)
        
        # Build Final Status
        status = SetupStatus.SUCCESS if tested else SetupStatus.SAVED_ONLY
        remark = f"{save_msg} | {events_msg} | {test_msg}"

        return SetupResult(
            atmid=dvr.atmid,
            ip=dvr.ip,
            port=dvr.port,
            model=model,
            status=status,
            remark=remark,
            duration_ms=int((time.time() - start) * 1000),
            timestamp=datetime.now(),
        )