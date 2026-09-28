import logging
import warnings
import aiomysql
from typing import List, Optional
from src.config import config
from src.models import DVR, SetupResult

warnings.filterwarnings("ignore", module="aiomysql")

logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        db_conf = config.get("database")
        self.pool = await aiomysql.create_pool(
            host=db_conf["host"],
            user=db_conf["user"],
            password=db_conf["password"],
            db=db_conf["database"],
            minsize=db_conf.get("pool_min", 5),
            maxsize=db_conf.get("pool_max", 20),
            autocommit=True,
            charset="utf8mb4",
        )
        await self._ensure_log_table()
        logger.info("Database pool initialized")

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()

    async def _ensure_log_table(self):
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                CREATE TABLE IF NOT EXISTS dvr_email_setup_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    atmid VARCHAR(50),
                    dvr_ip VARCHAR(50) NOT NULL,
                    dvr_port INT DEFAULT 81,
                    dvr_model VARCHAR(100),
                    status VARCHAR(30) NOT NULL,
                    remark TEXT,
                    duration_ms INT DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_atmid (atmid),
                    INDEX idx_ip (dvr_ip),
                    INDEX idx_status (status),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                """)
                # Ensure duration_ms exists if table was created previously without it
                await cur.execute("SHOW COLUMNS FROM dvr_email_setup_logs LIKE 'duration_ms'")
                has_duration = await cur.fetchone()
                if not has_duration:
                    await cur.execute("ALTER TABLE dvr_email_setup_logs ADD COLUMN duration_ms INT DEFAULT 0 AFTER remark")

                # Ensure uq_atmid index exists and clean up any past duplicates
                await cur.execute("SHOW INDEX FROM dvr_email_setup_logs WHERE Key_name = 'uq_atmid'")
                has_uq = await cur.fetchone()
                if not has_uq:
                    await cur.execute("""
                        DELETE t1 FROM dvr_email_setup_logs t1
                        INNER JOIN dvr_email_setup_logs t2
                        WHERE t1.id < t2.id AND t1.atmid = t2.atmid AND t1.atmid IS NOT NULL
                    """)
                    await cur.execute("ALTER TABLE dvr_email_setup_logs ADD UNIQUE KEY uq_atmid (atmid)")

    async def fetch_live_dvrs(self, limit=None) -> List[DVR]:
        defaults = config.get("dvr_defaults")
        query = "SELECT atmid, IPAddress, port, UserName, Password, dvrname FROM all_dvr_live WHERE live = 'Y' AND dvrname = 'hikvision'"
        if limit:
            query += f" LIMIT {limit}"

        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query)
                rows = await cur.fetchall()

        return [
            DVR(
                atmid=r.get("atmid", "N/A"),
                ip=r["IPAddress"],
                port=int(r.get("port") or defaults["port"]),
                username=r.get("UserName") or defaults["username"],
                password=r.get("Password") or defaults["password"],
                name=r.get("dvrname"),
            )
            for r in rows if r.get("IPAddress")
        ]

    async def find_dvr_by_ip(self, ip: str, port: int = 81) -> Optional[DVR]:
        defaults = config.get("dvr_defaults")
        query = "SELECT atmid, IPAddress, port, UserName, Password, dvrname FROM all_dvr_live WHERE IPAddress = %s LIMIT 1"
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, (ip,))
                row = await cur.fetchone()
                if row:
                    return DVR(
                        atmid=row.get("atmid", "N/A"),
                        ip=row["IPAddress"],
                        port=int(row.get("port") or port or defaults["port"]),
                        username=row.get("UserName") or defaults["username"],
                        password=row.get("Password") or defaults["password"],
                        name=row.get("dvrname"),
                    )
        return None

    async def insert_result(self, result: SetupResult):
        await self.bulk_insert_results([result])

    async def bulk_insert_results(self, results: List[SetupResult]):
        if not results:
            return
        sql = """
            INSERT INTO dvr_email_setup_logs
            (atmid, dvr_ip, dvr_port, dvr_model, status, remark, duration_ms, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                dvr_ip = VALUES(dvr_ip),
                dvr_port = VALUES(dvr_port),
                dvr_model = VALUES(dvr_model),
                status = VALUES(status),
                remark = VALUES(remark),
                duration_ms = VALUES(duration_ms),
                created_at = VALUES(created_at)
        """
        data = [
            (r.atmid, r.ip, r.port, r.model, r.status.value,
             r.remark, r.duration_ms, r.timestamp)
            for r in results
        ]
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(sql, data)
        logger.info(f"Saved/Updated {len(results)} log records")