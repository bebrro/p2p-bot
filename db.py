"""
PostgreSQL через asyncpg.
Railway автоматически задаёт DATABASE_URL когда добавляешь Postgres плагин.
Если DATABASE_URL нет — работаем в памяти (локальная разработка).
"""
import os
import logging
import asyncpg

logger = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None


async def init() -> None:
    """Вызвать один раз при старте бота."""
    global _pool
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        logger.warning("⚠️  DATABASE_URL не задан — данные хранятся только в памяти")
        return
    # Railway иногда даёт postgres:// вместо postgresql://
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    try:
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        await _create_tables()
        logger.info("✅ PostgreSQL подключён")
    except Exception as e:
        logger.error(f"❌ DB init error: {e}")
        _pool = None


def ok() -> bool:
    """True если БД доступна."""
    return _pool is not None


async def _create_tables() -> None:
    async with _pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT  NOT NULL,
            exchange   VARCHAR(20) NOT NULL,
            fiat       VARCHAR(10) NOT NULL,
            asset      VARCHAR(10) NOT NULL,
            threshold  FLOAT   NOT NULL,
            direction  VARCHAR(10) DEFAULT 'above',
            pay        VARCHAR(30) DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS whale_settings (
            user_id   BIGINT PRIMARY KEY,
            enabled   BOOLEAN DEFAULT FALSE,
            threshold FLOAT   DEFAULT 50000,
            fiats     TEXT    DEFAULT 'KZT',
            exchanges TEXT    DEFAULT 'binance,bybit',
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id          SERIAL PRIMARY KEY,
            exchange    VARCHAR(20) NOT NULL,
            fiat        VARCHAR(10) NOT NULL,
            asset       VARCHAR(10) NOT NULL,
            buy_price   FLOAT NOT NULL,
            sell_price  FLOAT NOT NULL,
            recorded_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_ph
            ON price_history(exchange, fiat, asset, recorded_at DESC);


        CREATE TABLE IF NOT EXISTS blacklist (
            user_id   BIGINT NOT NULL,
            nickname  TEXT   NOT NULL,
            added_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (user_id, nickname)
        );

        CREATE TABLE IF NOT EXISTS community_votes (
            nickname TEXT   NOT NULL,
            user_id  BIGINT NOT NULL,
            voted_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (nickname, user_id)
        );

        CREATE TABLE IF NOT EXISTS trackers (
            id               SERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL,
            exchange         VARCHAR(20) NOT NULL,
            fiat             VARCHAR(10) NOT NULL,
            asset            VARCHAR(10) NOT NULL,
            nickname         TEXT   NOT NULL,
            last_buy_price   FLOAT,
            last_sell_price  FLOAT,
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(user_id, exchange, fiat, asset, nickname)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id    BIGINT PRIMARY KEY,
            plan       VARCHAR(20) DEFAULT 'free',
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS exchange_accounts (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            exchange    VARCHAR(20) NOT NULL DEFAULT 'bybit',
            label       TEXT NOT NULL,
            api_key     TEXT NOT NULL,
            api_secret  TEXT NOT NULL,
            extra       TEXT DEFAULT '',
            nickname    TEXT DEFAULT '',
            is_active   BOOLEAN DEFAULT FALSE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id    BIGINT PRIMARY KEY,
            username   TEXT    DEFAULT '',
            first_seen TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id          SERIAL PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            referred_id BIGINT NOT NULL UNIQUE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id);

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id        BIGINT PRIMARY KEY,
            digest_enabled BOOLEAN DEFAULT TRUE,
            updated_at     TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS arb_alerts (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT NOT NULL,
            fiat       VARCHAR(10) NOT NULL,
            asset      VARCHAR(10) NOT NULL,
            threshold  FLOAT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(user_id, fiat, asset)
        );

        CREATE TABLE IF NOT EXISTS repricer_rules (
            id            SERIAL PRIMARY KEY,
            user_id       BIGINT NOT NULL,
            exchange      VARCHAR(20) NOT NULL DEFAULT 'bybit',
            ad_id         TEXT NOT NULL,
            fiat          VARCHAR(10) NOT NULL,
            asset         VARCHAR(10) NOT NULL,
            side          VARCHAR(10) NOT NULL,
            target_pos    INT DEFAULT 1,
            delta         FLOAT DEFAULT 0.5,
            min_price     FLOAT NOT NULL,
            max_price     FLOAT NOT NULL,
            current_price FLOAT,
            enabled       BOOLEAN DEFAULT TRUE,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(user_id, exchange, ad_id)
        );
        """)
        # Migrations — idempotent, safe to run on every startup
        await c.execute(
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS pay VARCHAR(30) DEFAULT ''"
        )


# ── Alerts ────────────────────────────────────────────────────────────────────

async def alerts_get(user_id: int) -> list[dict]:
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,exchange,fiat,asset,threshold,direction,pay FROM alerts "
            "WHERE user_id=$1 ORDER BY id",
            user_id,
        )
    return [dict(r) for r in rows]


async def alerts_delete(user_id: int, alert_id: int) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "DELETE FROM alerts WHERE user_id=$1 AND id=$2", user_id, alert_id
        )


async def alerts_add(user_id: int, exchange: str, fiat: str, asset: str,
                     threshold: float, direction: str = "above", pay: str = "") -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "INSERT INTO alerts(user_id,exchange,fiat,asset,threshold,direction,pay) "
            "VALUES($1,$2,$3,$4,$5,$6,$7)",
            user_id, exchange, fiat, asset, threshold, direction, pay,
        )


async def alerts_clear(user_id: int) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute("DELETE FROM alerts WHERE user_id=$1", user_id)


async def alerts_get_all() -> dict[int, list]:
    """Для фонового опроса в bot.py."""
    if not ok():
        return {}
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT user_id,exchange,fiat,asset,threshold,direction,pay FROM alerts ORDER BY user_id"
        )
    result: dict[int, list] = {}
    for r in rows:
        result.setdefault(r["user_id"], []).append(dict(r))
    return result


# ── Whale settings ────────────────────────────────────────────────────────────

async def whale_get(user_id: int) -> dict | None:
    if not ok():
        return None
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT enabled,threshold,fiats,exchanges FROM whale_settings WHERE user_id=$1",
            user_id,
        )
    if not row:
        return None
    d = dict(row)
    d["fiats"]     = d["fiats"].split(",")
    d["exchanges"] = d["exchanges"].split(",")
    return d


async def whale_save(user_id: int, enabled: bool, threshold: float,
                     fiats: list, exchanges: list) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO whale_settings(user_id,enabled,threshold,fiats,exchanges,updated_at)
            VALUES($1,$2,$3,$4,$5,NOW())
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=$2, threshold=$3, fiats=$4, exchanges=$5, updated_at=NOW()
        """,
            user_id, enabled, threshold,
            ",".join(fiats), ",".join(exchanges),
        )


async def whale_get_all_enabled() -> list[dict]:
    """Активные пользователи для фонового скана."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT user_id,threshold,fiats,exchanges FROM whale_settings WHERE enabled=TRUE"
        )
    result = []
    for r in rows:
        d = dict(r)
        d["fiats"]     = d["fiats"].split(",")
        d["exchanges"] = d["exchanges"].split(",")
        result.append(d)
    return result


# ── Price history ─────────────────────────────────────────────────────────────

async def history_add(exchange: str, fiat: str, asset: str,
                       buy: float, sell: float) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "INSERT INTO price_history(exchange,fiat,asset,buy_price,sell_price) "
            "VALUES($1,$2,$3,$4,$5)",
            exchange, fiat, asset, buy, sell,
        )
        # Удаляем точки старше 14 дней
        await c.execute(
            "DELETE FROM price_history WHERE exchange=$1 AND fiat=$2 AND asset=$3 "
            "AND recorded_at < NOW() - INTERVAL '14 days'",
            exchange, fiat, asset,
        )


async def history_get(exchange: str, fiat: str, asset: str,
                       limit: int = 672) -> list[tuple]:
    """Возвращает [(datetime, buy, sell), ...] от старого к новому."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT recorded_at,buy_price,sell_price FROM price_history "
            "WHERE exchange=$1 AND fiat=$2 AND asset=$3 "
            "ORDER BY recorded_at DESC LIMIT $4",
            exchange, fiat, asset, limit,
        )
    return [(r["recorded_at"], r["buy_price"], r["sell_price"]) for r in reversed(rows)]


# ── Blacklist ─────────────────────────────────────────────────────────────────

async def blacklist_get(user_id: int) -> set[str]:
    if not ok():
        return set()
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT nickname FROM blacklist WHERE user_id=$1", user_id
        )
    return {r["nickname"] for r in rows}


async def blacklist_add(user_id: int, nickname: str) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "INSERT INTO blacklist(user_id,nickname) VALUES($1,$2) ON CONFLICT DO NOTHING",
            user_id, nickname,
        )


async def blacklist_remove(user_id: int, nickname: str) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "DELETE FROM blacklist WHERE user_id=$1 AND nickname=$2", user_id, nickname
        )


async def community_vote(user_id: int, nickname: str) -> int:
    """Добавляет голос, возвращает итоговое кол-во."""
    if not ok():
        return 0
    async with _pool.acquire() as c:
        await c.execute(
            "INSERT INTO community_votes(nickname,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
            nickname, user_id,
        )
        count = await c.fetchval(
            "SELECT COUNT(*) FROM community_votes WHERE nickname=$1", nickname
        )
    return int(count)


async def community_get_all() -> list[tuple]:
    """[(nickname, vote_count), ...] по убыванию голосов."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            SELECT nickname, COUNT(*) AS cnt
            FROM community_votes GROUP BY nickname ORDER BY cnt DESC
        """)
    return [(r["nickname"], int(r["cnt"])) for r in rows]


# ── Trackers ──────────────────────────────────────────────────────────────────

async def trackers_get(user_id: int) -> list[dict]:
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,exchange,fiat,asset,nickname,last_buy_price,last_sell_price "
            "FROM trackers WHERE user_id=$1 ORDER BY id",
            user_id,
        )
    return [_tracker_row(r) for r in rows]


async def trackers_add(user_id: int, exchange: str, fiat: str, asset: str,
                        nickname: str, buy_price: float | None = None,
                        sell_price: float | None = None) -> int:
    """Возвращает id новой записи."""
    if not ok():
        return -1
    async with _pool.acquire() as c:
        row = await c.fetchrow("""
            INSERT INTO trackers(user_id,exchange,fiat,asset,nickname,last_buy_price,last_sell_price)
            VALUES($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT(user_id,exchange,fiat,asset,nickname) DO UPDATE SET
                last_buy_price=$6, last_sell_price=$7
            RETURNING id
        """, user_id, exchange, fiat, asset, nickname, buy_price, sell_price)
    return row["id"]


async def trackers_delete(user_id: int, tracker_id: int) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "DELETE FROM trackers WHERE user_id=$1 AND id=$2", user_id, tracker_id
        )


async def trackers_update_prices(tracker_id: int,
                                   buy_price: float | None,
                                   sell_price: float | None) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE trackers SET last_buy_price=$1,last_sell_price=$2 WHERE id=$3",
            buy_price, sell_price, tracker_id,
        )


async def trackers_get_all() -> list[dict]:
    """Все трекеры всех пользователей для фонового опроса."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,user_id,exchange,fiat,asset,nickname,last_buy_price,last_sell_price "
            "FROM trackers ORDER BY user_id,id"
        )
    return [_tracker_row(r, include_uid=True) for r in rows]


async def blacklist_get_all() -> dict:
    """Все личные ЧС: {user_id: set(nicknames)}. Для загрузки в память при старте."""
    if not ok():
        return {}
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT user_id, nickname FROM blacklist")
    result: dict[int, set] = {}
    for r in rows:
        result.setdefault(r["user_id"], set()).add(r["nickname"])
    return result


async def community_get_all_votes_raw() -> list:
    """[(nickname, user_id), ...] — все голоса краудсорса."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT nickname, user_id FROM community_votes")
    return [(r["nickname"], r["user_id"]) for r in rows]


def _tracker_row(r, include_uid: bool = False) -> dict:
    d = dict(r)
    bp = d.pop("last_buy_price",  None)
    sp = d.pop("last_sell_price", None)
    d["buy"]  = {"price": bp, "available": 0} if bp is not None else None
    d["sell"] = {"price": sp, "available": 0} if sp is not None else None
    if not include_uid:
        d.pop("user_id", None)
    return d


# ── Exchange accounts ──────────────────────────────────────────────────────────

async def accounts_get(user_id: int) -> list[dict]:
    """Все аккаунты пользователя."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,exchange,label,api_key,api_secret,extra,nickname,is_active "
            "FROM exchange_accounts WHERE user_id=$1 ORDER BY id",
            user_id,
        )
    return [dict(r) for r in rows]


async def accounts_add(user_id: int, exchange: str, label: str,
                        api_key: str, api_secret: str,
                        extra: str = "", nickname: str = "") -> int:
    """Добавляет аккаунт, возвращает id."""
    if not ok():
        return -1
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "INSERT INTO exchange_accounts"
            "(user_id,exchange,label,api_key,api_secret,extra,nickname,is_active) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,FALSE) RETURNING id",
            user_id, exchange, label, api_key, api_secret, extra, nickname,
        )
    return row["id"]


async def accounts_delete(user_id: int, account_id: int) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "DELETE FROM exchange_accounts WHERE user_id=$1 AND id=$2",
            user_id, account_id,
        )


async def accounts_set_active(user_id: int, exchange: str, account_id: int) -> None:
    """Активирует один аккаунт (для данной биржи), деактивирует остальные."""
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE exchange_accounts SET is_active=FALSE "
            "WHERE user_id=$1 AND exchange=$2",
            user_id, exchange,
        )
        await c.execute(
            "UPDATE exchange_accounts SET is_active=TRUE "
            "WHERE user_id=$1 AND id=$2",
            user_id, account_id,
        )


async def accounts_get_all() -> list[dict]:
    """Все аккаунты всех пользователей — для загрузки в память при старте."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,user_id,exchange,label,api_key,api_secret,extra,nickname,is_active "
            "FROM exchange_accounts ORDER BY user_id,id"
        )
    return [dict(r) for r in rows]


# ── Repricer rules ─────────────────────────────────────────────────────────────

async def repricer_get(user_id: int) -> list[dict]:
    """Все правила переценки пользователя."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,exchange,ad_id,fiat,asset,side,target_pos,delta,"
            "min_price,max_price,current_price,enabled "
            "FROM repricer_rules WHERE user_id=$1 ORDER BY id",
            user_id,
        )
    return [dict(r) for r in rows]


async def repricer_add(user_id: int, exchange: str, ad_id: str,
                        fiat: str, asset: str, side: str,
                        target_pos: int, delta: float,
                        min_price: float, max_price: float,
                        current_price: float | None = None) -> int:
    """Добавляет правило, возвращает id."""
    if not ok():
        return -1
    async with _pool.acquire() as c:
        row = await c.fetchrow("""
            INSERT INTO repricer_rules
            (user_id,exchange,ad_id,fiat,asset,side,target_pos,delta,min_price,max_price,current_price,enabled)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE)
            ON CONFLICT(user_id,exchange,ad_id) DO UPDATE SET
                target_pos=$7, delta=$8, min_price=$9, max_price=$10, enabled=TRUE
            RETURNING id
        """, user_id, exchange, ad_id, fiat, asset, side,
             target_pos, delta, min_price, max_price, current_price)
    return row["id"]


async def repricer_delete(user_id: int, rule_id: int) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "DELETE FROM repricer_rules WHERE user_id=$1 AND id=$2",
            user_id, rule_id,
        )


async def repricer_update_price(rule_id: int, current_price: float) -> None:
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute(
            "UPDATE repricer_rules SET current_price=$1 WHERE id=$2",
            current_price, rule_id,
        )


async def repricer_get_all_enabled() -> list[dict]:
    """Все включённые правила всех пользователей для фонового цикла."""
    if not ok():
        return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,user_id,exchange,ad_id,fiat,asset,side,target_pos,delta,"
            "min_price,max_price,current_price "
            "FROM repricer_rules WHERE enabled=TRUE ORDER BY user_id,id"
        )
    return [dict(r) for r in rows]


# ── Users ─────────────────────────────────────────────────────────────────────

async def user_register(user_id: int, username: str = "") -> bool:
    """Регистрирует пользователя. Возвращает True если это первый визит."""
    if not ok(): return False
    async with _pool.acquire() as c:
        res = await c.execute(
            "INSERT INTO users(user_id, username) VALUES($1, $2) ON CONFLICT DO NOTHING",
            user_id, username or "",
        )
    return res == "INSERT 0 1"  # True = новый пользователь


async def users_get_all_ids() -> list[int]:
    """Все зарегистрированные user_id (для рассылки)."""
    if not ok(): return []
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT user_id FROM users")
    return [r["user_id"] for r in rows]


# ── Referrals ──────────────────────────────────────────────────────────────────

async def referral_add(referrer_id: int, referred_id: int) -> bool:
    """Сохраняет реферал. False если пользователь уже был кем-то приглашён."""
    if not ok(): return False
    async with _pool.acquire() as c:
        res = await c.execute(
            "INSERT INTO referrals(referrer_id, referred_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
            referrer_id, referred_id,
        )
    return res == "INSERT 0 1"


async def referral_count(referrer_id: int) -> int:
    """Количество приглашённых пользователем."""
    if not ok(): return 0
    async with _pool.acquire() as c:
        count = await c.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", referrer_id
        )
    return int(count or 0)


# ── User settings ──────────────────────────────────────────────────────────────

async def user_settings_get(user_id: int) -> dict:
    """Настройки пользователя (с дефолтами)."""
    if not ok(): return {"digest_enabled": True}
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT digest_enabled FROM user_settings WHERE user_id=$1", user_id
        )
    return dict(row) if row else {"digest_enabled": True}


async def user_settings_set(user_id: int, digest_enabled: bool) -> None:
    if not ok(): return
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO user_settings(user_id, digest_enabled, updated_at)
            VALUES($1, $2, NOW())
            ON CONFLICT(user_id) DO UPDATE SET digest_enabled=$2, updated_at=NOW()
        """, user_id, digest_enabled)


async def users_get_digest_recipients() -> list[int]:
    """user_id всех кто хочет дайджест (по умолчанию — все)."""
    if not ok(): return []
    async with _pool.acquire() as c:
        # Все зарегистрированные пользователи у которых НЕ выключен дайджест
        rows = await c.fetch("""
            SELECT u.user_id FROM users u
            LEFT JOIN user_settings s ON s.user_id = u.user_id
            WHERE s.digest_enabled IS DISTINCT FROM FALSE
        """)
    return [r["user_id"] for r in rows]


# ── Arb Alerts ────────────────────────────────────────────────────────────────

async def arb_alerts_get(user_id: int) -> list[dict]:
    if not ok(): return []
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, fiat, asset, threshold FROM arb_alerts "
            "WHERE user_id=$1 ORDER BY id",
            user_id,
        )
    return [dict(r) for r in rows]


async def arb_alerts_add(user_id: int, fiat: str, asset: str, threshold: float) -> int:
    """Добавляет арбитражный алерт, возвращает id."""
    if not ok(): return -1
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "INSERT INTO arb_alerts(user_id,fiat,asset,threshold) VALUES($1,$2,$3,$4) "
            "ON CONFLICT(user_id,fiat,asset) DO UPDATE SET threshold=$4 RETURNING id",
            user_id, fiat, asset, threshold,
        )
    return row["id"]


async def arb_alerts_delete_by_id(user_id: int, alert_id: int) -> None:
    if not ok(): return
    async with _pool.acquire() as c:
        await c.execute(
            "DELETE FROM arb_alerts WHERE user_id=$1 AND id=$2", user_id, alert_id
        )


async def arb_alerts_get_all() -> dict[int, list]:
    """Все арбитражные алерты всех пользователей."""
    if not ok(): return {}
    async with _pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, user_id, fiat, asset, threshold FROM arb_alerts ORDER BY user_id, id"
        )
    result: dict[int, list] = {}
    for r in rows:
        result.setdefault(r["user_id"], []).append({
            "db_id":    r["id"],
            "fiat":     r["fiat"],
            "asset":    r["asset"],
            "threshold": r["threshold"],
        })
    return result


# ── Subscriptions ──────────────────────────────────────────────────────────────

async def subscription_get(user_id: int) -> dict | None:
    """Возвращает подписку пользователя или None если нет."""
    if not ok():
        return None
    async with _pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT plan, expires_at FROM subscriptions WHERE user_id=$1", user_id
        )
    return dict(row) if row else None


async def subscription_set(user_id: int, plan: str, expires_at=None) -> None:
    """Создаёт или обновляет подписку."""
    if not ok():
        return
    async with _pool.acquire() as c:
        await c.execute("""
            INSERT INTO subscriptions(user_id, plan, expires_at, updated_at)
            VALUES($1, $2, $3, NOW())
            ON CONFLICT(user_id) DO UPDATE SET plan=$2, expires_at=$3, updated_at=NOW()
        """, user_id, plan, expires_at)


async def subscription_get_all() -> dict[int, dict]:
    """Все подписки всех пользователей."""
    if not ok():
        return {}
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT user_id, plan, expires_at FROM subscriptions")
    return {r["user_id"]: dict(r) for r in rows}
