"""数据库初始化模块。

负责应用启动时的数据库初始化操作：
1. 创建所有表（基于 ORM 模型定义）
2. 填充默认种子数据（管理员账户、默认策略/风控参数、品种映射等）

注意：数据库结构迁移（Schema Migration）由 Alembic 管理，
     本模块不再包含手工 ALTER TABLE 逻辑。
     如需修改表结构，请使用：alembic revision --autogenerate -m "描述"
"""

from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password
from app.config.settings import INSECURE_DEFAULT_ADMIN_PASSWORD, get_settings, runtime_requires_strong_secrets
from app.db.models import Base, RiskSetting, StrategySetting, SymbolMapping, SystemSetting, User
from app.db.session import engine
from app.accounts.sync import ensure_initial_account_snapshots
from app.market.symbols import seed_symbol_mappings_from_file


def init_db() -> None:
    """初始化数据库：创建所有表并填充默认数据。

    在应用启动时调用。create_all 仅创建尚不存在的表，
    不会修改已有表结构（结构变更请使用 Alembic）。
    """
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        seed_defaults(db)


def seed_defaults(db: Session) -> None:
    """填充系统默认种子数据。

    仅在对应数据不存在时才插入，不会覆盖已有数据：
    1. 管理员用户：从配置文件读取用户名/密码，首次启动时创建
    2. 策略参数：使用默认值或配置文件中的执行模式
    3. 风控参数：使用默认值
    4. 实盘开关：默认关闭
    5. 账户快照：确保各平台有初始快照记录
    6. 品种映射：从配置文件加载种子数据（仅首次启动）

    安全提示：
        在生产/实盘模式下，若管理员仍使用默认密码，将抛出异常阻止启动。
    """
    settings = get_settings()
    security = settings.security

    # 1. 创建管理员用户（若不存在）
    admin_user = db.query(User).filter(User.username == security.admin_username).first()
    if not admin_user:
        db.add(
            User(
                username=security.admin_username,
                password_hash=hash_password(security.admin_password),
                role="admin",
            )
        )
    elif runtime_requires_strong_secrets(settings) and verify_password(
        INSECURE_DEFAULT_ADMIN_PASSWORD, admin_user.password_hash
    ):
        raise RuntimeError(
            "不安全启动配置：数据库中的管理员账号仍使用默认密码。"
            "请先重置管理员密码后再以生产或实盘相关模式启动。"
        )

    # 2. 创建默认策略参数
    if not db.query(StrategySetting).first():
        db.add(StrategySetting(execution_mode=security.default_execution_mode))

    # 3. 创建默认风控参数
    if not db.query(RiskSetting).first():
        db.add(RiskSetting())

    # 4. 初始化实盘交易开关（默认关闭）
    if not db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first():
        db.add(SystemSetting(key="live_trading_enabled", value="false"))

    db.commit()

    # 5. 确保各平台有初始账户快照
    ensure_initial_account_snapshots(db)

    # 6. 品种映射种子数据（仅首次启动时从配置文件导入）
    #    后续增删改都以数据库为准，避免重启覆盖前端保存的映射
    seed_flag = db.query(SystemSetting).filter(SystemSetting.key == "symbol_mappings_seeded").first()
    if not seed_flag:
        if not db.query(SymbolMapping).first():
            seed_symbol_mappings_from_file(db)
        db.add(SystemSetting(key="symbol_mappings_seeded", value="true"))
        db.commit()
