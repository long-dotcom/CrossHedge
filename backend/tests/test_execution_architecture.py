"""交易副作用边界的静态架构守卫。"""

from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _python_sources() -> list[Path]:
    return sorted(APP_ROOT.rglob("*.py"))


def test_api_and_business_services_cannot_call_venue_submit_directly() -> None:
    """外部下单只能由 Outbox Worker 调用统一连接器协议。"""
    callers = {
        path.relative_to(APP_ROOT).as_posix()
        for path in _python_sources()
        if ".submit_order(" in path.read_text(encoding="utf-8")
    }
    business_callers = {path for path in callers if not path.startswith("venues/")}
    assert business_callers == {"execution/outbox_worker.py"}


def test_removed_third_party_runtime_cannot_reappear() -> None:
    forbidden = "nauti" + "lus_trader"
    assert all(forbidden not in path.read_text(encoding="utf-8").lower() for path in _python_sources())


def test_removed_execution_engines_cannot_reappear() -> None:
    removed = {
        "execution/engine.py",
        "execution/close_service.py",
        "execution/manual_resolution.py",
        "execution/persistence.py",
        "execution/probe.py",
    }
    existing = {path.relative_to(APP_ROOT).as_posix() for path in _python_sources()}
    assert removed.isdisjoint(existing)
