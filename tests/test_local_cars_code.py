import unittest
from unittest.mock import patch

from starlette.requests import Request

import config
from security import APIException, get_car_credentials


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/booking-options"})


class LocalCarsCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_account_uses_server_configured_code(self):
        with (
            patch.object(config, "CARS_PERSONAL_CODE", "  local-personal-code  "),
            patch.object(config, "CARS_WORK_CODE", " local-work-code "),
        ):
            personal = await get_car_credentials(_request(), account="personal")
            work = await get_car_credentials(_request(), account="work")

        self.assertEqual((personal.account, personal.code), ("personal", "local-personal-code"))
        self.assertEqual((work.account, work.code), ("work", "local-work-code"))

    async def test_missing_selected_account_code_fails_safely(self):
        with (
            patch.object(config, "CARS_PERSONAL_CODE", ""),
            patch.object(config, "CARS_WORK_CODE", "configured-work-code"),
            self.assertRaises(APIException) as raised,
        ):
            await get_car_credentials(_request(), account="personal")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "credentials_missing")
        self.assertNotIn("configured-work-code", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
