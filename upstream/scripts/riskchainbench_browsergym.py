#!/usr/bin/env python3
"""BrowserGym + Playwright adapter for RiskChainBench Task 2.

BrowserGym owns browser lifecycle, browser observations, element BIDs, and
high-level action execution. RiskChainBench retains only benchmark-specific
network isolation, local-runtime telemetry, and evidence protocol state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
from typing import Any
from urllib.parse import urlparse

from browsergym.core.action.highlevel import HighLevelActionSet
from browsergym.core.env import BrowserEnv
from browsergym.core.task import AbstractBrowserTask
from playwright.sync_api import Locator, Page


EXPECTED_BROWSERGYM_CORE_VERSION = "0.14.3"
EXPECTED_PLAYWRIGHT_VERSION = "1.44.0"
BROWSERGYM_CORE_VERSION = version("browsergym-core")
PLAYWRIGHT_VERSION = version("playwright")
BROWSER_HARNESS_ID = "riskchainbench-browsergym-playwright/v0.1"
BROWSERGYM_ID_ATTRIBUTE = "bid"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check(bid: str):
    """Set a checkbox or radio control to checked.

    Examples:
        check("12")
    """

    elem = get_elem_by_bid(page, bid, scroll_into_view=True)
    elem.check(timeout=500)


def submit_form(bid: str):
    """Submit the form represented by, or containing, an element.

    Examples:
        submit_form("12")
    """

    elem = get_elem_by_bid(page, bid, scroll_into_view=True)
    submitted = elem.evaluate(
        """element => {
          const form = element instanceof HTMLFormElement
            ? element
            : element.closest('form');
          if (!form) return false;
          const isSubmitter =
            (element instanceof HTMLButtonElement &&
             (!element.type || element.type === 'submit')) ||
            (element instanceof HTMLInputElement &&
             ['submit', 'image'].includes(element.type));
          if (isSubmitter) {
            element.click();
          } else {
            form.requestSubmit();
          }
          return true;
        }"""
    )
    if not submitted:
        raise ValueError(f'Could not find a form for bid "{bid}"')


def reload_page():
    """Reload the active page.

    Examples:
        reload_page()
    """

    page.reload(wait_until="domcontentloaded")


def select_first_option(bid: str):
    """Select the first enabled non-empty option.

    Examples:
        select_first_option("12")
    """

    elem = get_elem_by_bid(page, bid, scroll_into_view=True)
    option_value = elem.evaluate(
        """element => {
          const option = Array.from(element.options || []).find(
            row => !row.disabled && String(row.value || '').length > 0
          );
          return option ? option.value : null;
        }"""
    )
    if option_value is None:
        raise ValueError(f'Could not find an eligible option for bid "{bid}"')
    elem.select_option(option_value, timeout=500)


def build_action_set() -> HighLevelActionSet:
    """Return the frozen single-action BrowserGym action set."""

    return HighLevelActionSet(
        subsets=["bid", "custom"],
        custom_actions=[check, submit_form, reload_page, select_first_option],
        multiaction=False,
        demo_mode="off",
        strict=True,
        retry_with_force=False,
    )


def assert_frozen_browser_runtime() -> None:
    """Reject dependency drift in formal benchmark runs."""

    observed = {
        "browsergym-core": BROWSERGYM_CORE_VERSION,
        "playwright": PLAYWRIGHT_VERSION,
    }
    expected = {
        "browsergym-core": EXPECTED_BROWSERGYM_CORE_VERSION,
        "playwright": EXPECTED_PLAYWRIGHT_VERSION,
    }
    if observed != expected:
        raise RuntimeError(
            "frozen browser runtime mismatch: "
            f"expected={expected!r}, observed={observed!r}"
        )


class RiskChainLocalTask(AbstractBrowserTask):
    """A neutral local-only BrowserGym task used by the fixed harness."""

    @classmethod
    def get_task_id(cls) -> str:
        return "riskchainbench-local-evidence-investigation-v0.1"

    def __init__(
        self,
        seed: int,
        *,
        start_url: str,
        allowed_port: int,
        viewport: dict[str, int],
        timeout_ms: int,
        external_attempts: list[str],
        responses: list[dict[str, Any]],
        console_errors: list[str],
        page_errors: list[str],
    ) -> None:
        super().__init__(seed)
        self.start_url = start_url
        self.allowed_port = allowed_port
        self.viewport = dict(viewport)
        self.slow_mo = 0
        self.timeout = timeout_ms
        self.external_attempts = external_attempts
        self.responses = responses
        self.console_errors = console_errors
        self.page_errors = page_errors

    def setup(self, page: Page) -> tuple[str, dict[str, Any]]:
        def route_handler(route: Any) -> None:
            parsed = urlparse(route.request.url)
            local = (
                parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.allowed_port
            )
            if local or parsed.scheme in {"data", "blob", "about"}:
                route.continue_()
                return
            self.external_attempts.append(_sha256_text(route.request.url))
            route.abort("blockedbyclient")

        def record_response(response: Any) -> None:
            parsed = urlparse(response.url)
            if (
                parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.allowed_port
            ):
                self.responses.append(
                    {
                        "url": response.url,
                        "path": parsed.path,
                        "method": response.request.method,
                        "status": response.status,
                        "resource_type": response.request.resource_type,
                    }
                )

        page.context.route("**/*", route_handler)
        page.on("response", record_response)
        page.on(
            "console",
            lambda message: self.console_errors.append(message.text[:500])
            if message.type == "error"
            else None,
        )
        page.on(
            "pageerror",
            lambda error: self.page_errors.append(str(error)[:500]),
        )
        response = page.goto(
            self.start_url,
            wait_until="domcontentloaded",
            timeout=self.timeout,
        )
        if response is None:
            raise RuntimeError("BrowserGym local task returned no initial response")
        return (
            "Inspect the isolated local website and collect grounded policy evidence.",
            {
                "harness_id": BROWSER_HARNESS_ID,
                "local_replay_only": True,
            },
        )

    def validate(
        self,
        page: Page,
        chat_messages: list[str],
    ) -> tuple[float, bool, str, dict[str, Any]]:
        del page, chat_messages
        return 0.0, False, "", {"semantic_reward_disabled": True}


@dataclass(frozen=True)
class BrowserGymStep:
    action: str
    observation: dict[str, Any]
    reward: float
    terminated: bool
    truncated: bool
    info: dict[str, Any]
    action_error: str


class BrowserGymSession:
    """Own one BrowserGym episode and expose a narrow runner-facing API."""

    def __init__(
        self,
        *,
        start_url: str,
        allowed_port: int,
        viewport: dict[str, int],
        timeout_ms: int,
        external_attempts: list[str],
        responses: list[dict[str, Any]],
        console_errors: list[str],
        page_errors: list[str],
    ) -> None:
        self.action_set = build_action_set()
        self.env = BrowserEnv(
            task_entrypoint=RiskChainLocalTask,
            task_kwargs={
                "start_url": start_url,
                "allowed_port": allowed_port,
                "viewport": viewport,
                "timeout_ms": timeout_ms,
                "external_attempts": external_attempts,
                "responses": responses,
                "console_errors": console_errors,
                "page_errors": page_errors,
            },
            viewport=None,
            slow_mo=None,
            timeout=None,
            tags_to_mark="standard_html",
            headless=True,
            terminate_on_infeasible=False,
            action_mapping=self.action_set.to_python_code,
            use_raw_page_output=False,
            pre_observation_delay=0.25,
            pw_context_kwargs={
                "device_scale_factor": 1,
                "service_workers": "block",
            },
        )
        self.observation: dict[str, Any] | None = None

    @property
    def page(self) -> Page:
        return self.env.page

    @property
    def context(self) -> Any:
        return self.env.context

    def reset(self, *, seed: int = 0) -> dict[str, Any]:
        observation, _ = self.env.reset(seed=seed)
        self.observation = observation
        return observation

    def step(self, action: str) -> BrowserGymStep:
        observation, reward, terminated, truncated, info = self.env.step(action)
        self.observation = observation
        return BrowserGymStep(
            action=action,
            observation=observation,
            reward=float(reward),
            terminated=bool(terminated),
            truncated=bool(truncated),
            info=info,
            action_error=str(observation.get("last_action_error") or ""),
        )

    def refresh(self) -> dict[str, Any]:
        step = self.step("noop(0)")
        if step.action_error:
            raise RuntimeError(f"BrowserGym refresh failed: {step.action_error}")
        return step.observation

    def close(self) -> None:
        self.env.close()


def locator_for_bid(page: Page, bid: str) -> Locator:
    """Locate a BrowserGym BID, including BIDs nested in frames."""

    from browsergym.core.action.utils import get_elem_by_bid

    return get_elem_by_bid(page, bid, scroll_into_view=False)


def browsergym_action(
    *,
    action: str,
    bid: str | None,
    value: str | None = None,
    select_first: bool = False,
) -> str:
    """Translate the frozen RiskChainBench action into BrowserGym syntax."""

    if action == "fill":
        if bid is None:
            raise ValueError("fill requires a BrowserGym BID")
        if select_first:
            return f"select_first_option({bid!r})"
        if value is None:
            raise ValueError("fill requires a controller value")
        return f"fill({bid!r}, {value!r})"
    if action == "check":
        if bid is None:
            raise ValueError("check requires a BrowserGym BID")
        return f"check({bid!r})"
    if action == "click":
        if bid is None:
            raise ValueError("click requires a BrowserGym BID")
        return f"click({bid!r})"
    if action == "submit":
        if bid is None:
            raise ValueError("submit requires a BrowserGym BID")
        return f"submit_form({bid!r})"
    if action == "reload":
        return "reload_page()"
    if action == "scroll_down":
        return "scroll(0, 700)"
    if action == "scroll_up":
        return "scroll(0, -700)"
    raise ValueError(f"unsupported BrowserGym action: {action}")
