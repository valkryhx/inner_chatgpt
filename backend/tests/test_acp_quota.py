import asyncio

import pytest

from app.quota import Limits, QuotaExceeded, QuotaManager


def test_second_acp_lease_is_rejected_for_same_user():
    async def scenario():
        q = QuotaManager(Limits(user_acp_active=1, global_acp_active=2))
        async with q.acp_active("u1", timeout_s=0.05):
            with pytest.raises(QuotaExceeded):
                async with q.acp_active("u1", timeout_s=0.05):
                    pass

    asyncio.run(scenario())


def test_other_user_is_not_blocked():
    async def scenario():
        q = QuotaManager(Limits(user_acp_active=1, global_acp_active=2))
        async with q.acp_active("u1", timeout_s=0.05):
            async with q.acp_active("u2", timeout_s=0.05):
                return True

    assert asyncio.run(scenario())


def test_global_limit():
    async def scenario():
        q = QuotaManager(Limits(user_acp_active=1, global_acp_active=1))
        async with q.acp_active("u1", timeout_s=0.05):
            with pytest.raises(QuotaExceeded):
                async with q.acp_active("u2", timeout_s=0.05):
                    pass

    asyncio.run(scenario())
