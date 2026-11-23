"""Run only on an explicitly enabled, billable Flash deployment."""
import os

import pytest

from dovideo.infrastructure.providers.flash_preflight import (
    flash_config, report_path, run_preflight, save_report,
)
from dovideo.infrastructure.providers.http import StdlibAsyncJsonPostClient


@pytest.mark.live_provider
@pytest.mark.asyncio
async def test_live_flash():
    if os.environ.get("DOVIDEO_RUN_LIVE_FLASH") != "1":
        pytest.skip("Opt in with DOVIDEO_RUN_LIVE_FLASH=1 and supply this deployment's credential")
    report = await run_preflight(flash_config(os.environ), StdlibAsyncJsonPostClient())
    save_report(report_path(os.environ), report)
    assert report["status"] == "PASSED", report["issues"]
