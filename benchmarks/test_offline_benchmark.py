from pathlib import Path

import pytest

from benchmarks.offline_benchmark import run


@pytest.mark.asyncio
async def test_offline_repeat_sends_zero_bytes_to_vps(tmp_path):
    result = await run(parts=4, part_size=128 * 1024, parallel=2, root=tmp_path)
    assert result["first_vps_body_bytes"] == 4 * 128 * 1024
    assert result["repeat_vps_body_bytes"] == 0
    assert result["cache_files"] == 4
    assert result["mock_r2_puts"] == 8
    assert result["real_bandwidth_tested"] is False
