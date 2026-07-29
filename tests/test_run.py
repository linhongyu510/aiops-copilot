import asyncio

from app import run


def test_configure_event_loop_is_safe_on_current_platform(monkeypatch) -> None:
    installed = []
    monkeypatch.setattr(run.os, "name", "nt")
    monkeypatch.setattr(
        run.asyncio,
        "WindowsSelectorEventLoopPolicy",
        lambda: "selector-policy",
        raising=False,
    )
    monkeypatch.setattr(
        run.asyncio,
        "set_event_loop_policy",
        lambda policy: installed.append(policy),
    )

    run.configure_event_loop()

    assert installed == ["selector-policy"]
    asyncio.set_event_loop_policy(None)


def test_selector_loop_factory_returns_selector_loop() -> None:
    loop = run.selector_loop_factory()
    assert isinstance(loop, asyncio.SelectorEventLoop)
    loop.close()
