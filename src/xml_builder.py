from typing import List, Dict
from src.config import config


class XMLBuilder:
    @staticmethod
    def _receivers_xml(receivers: List[Dict]) -> str:
        xml_parts = []
        rec_list = receivers or []
        # Support up to 3 receiver slots standard in Hikvision DVRs
        for idx in range(1, 4):
            slot_id = str(idx)
            matched = next((r for r in rec_list if str(r.get("id")) == slot_id), None)
            if matched and matched.get("email"):
                xml_parts.append(
                    f"<receiver><id>{slot_id}</id>"
                    f"<name>{matched.get('name', '')}</name>"
                    f"<emailAddress>{matched['email']}</emailAddress></receiver>"
                )
            else:
                # Explicitly empty unused slots so previous emails are wiped from DVR
                xml_parts.append(f"<receiver><id>{slot_id}</id><name/><emailAddress/></receiver>")
        return "".join(xml_parts)

    @classmethod
    def email_config(cls, dvr_ip: str) -> str:
        smtp = config.get("smtp")
        receivers = config.get("receivers")
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<mailing version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<sender>
<name>Hikvision {dvr_ip}</name>
<emailAddress>{smtp['sender_email']}</emailAddress>
<smtp>
<enableAuthorization>true</enableAuthorization>
<enableSSL>{str(smtp['enable_ssl']).lower()}</enableSSL>
<addressingFormatType>hostname</addressingFormatType>
<hostName>{smtp['server']}</hostName>
<portNo>{smtp['port']}</portNo>
<accountName>{smtp['sender_email']}</accountName>
<password>{smtp['password']}</password>
</smtp>
</sender>
<receiverList>{cls._receivers_xml(receivers)}</receiverList>
<attachment>
<snapshot><enabled>true</enabled><interval>2</interval></snapshot>
</attachment>
</mailing>"""

    @classmethod
    def test_email(cls, dvr_ip: str) -> str:
        smtp = config.get("smtp")
        receivers = config.get("receivers")
        # Hikvision mailingTestDescription expects a single receiver
        first_receiver = receivers[:1] if receivers else []
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<mailingTestDescription version="1.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<sendName>Hikvision {dvr_ip}</sendName>
<sendEmailAddress>{smtp['sender_email']}</sendEmailAddress>
<addressingFormatType>hostname</addressingFormatType>
<hostName>{smtp['server']}</hostName>
<portNo>{smtp['port']}</portNo>
<enableSSL>{str(smtp['enable_ssl']).lower()}</enableSSL>
<enableAuthorization>true</enableAuthorization>
<accountName>{smtp['sender_email']}</accountName>
<password>{smtp['password']}</password>
<receiverList>{cls._receivers_xml(first_receiver)}</receiverList>
</mailingTestDescription>"""