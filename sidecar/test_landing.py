from __future__ import annotations

import unittest

from starlette.requests import Request

import server


def request(*, owner: bool = False, query: bytes = b"") -> Request:
    headers = [(b"x-openhost-is-owner", b"true")] if owner else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": query,
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("capsule.example", 443),
        }
    )


class LandingTests(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_visitors_see_public_landing(self) -> None:
        response = await server.landing(request())

        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("protocol-specification.gmi", body)
        self.assertIn("/feed.rss", body)
        self.assertIn("Lagrange", body)
        self.assertNotIn("A quieter place", body)
        self.assertNotIn("About the certificate", body)
        self.assertNotIn("\u2014", body)

    async def test_owner_is_redirected_to_editor(self) -> None:
        response = await server.landing(request(owner=True))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/edit")

    async def test_owner_can_explicitly_view_public_page(self) -> None:
        response = await server.landing(request(owner=True, query=b"public=1"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Protocol specification", response.body.decode())


if __name__ == "__main__":
    unittest.main()
