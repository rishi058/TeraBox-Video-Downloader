import unittest
from unittest.mock import Mock, patch

from teraboxDL import terabox_dl


class TeraBoxProxyMetadataTests(unittest.TestCase):
    def _metadata_from(self, proxy_response):
        response = Mock(status_code=200)
        response.json.return_value = proxy_response

        with (
            patch.object(terabox_dl, "PROXY_URL", "https://proxy.test"),
            patch.object(terabox_dl, "THIRD_PARTY_TERABOXDL_URL", "https://provider.test"),
            patch.object(terabox_dl.random, "uniform", return_value=0),
            patch.object(terabox_dl.time, "sleep"),
            patch.object(terabox_dl.requests, "post", return_value=response) as post,
        ):
            result = terabox_dl._get_video_metadata("https://terabox.com/s/1example")
            self.proxy_payload = post.call_args.kwargs["json"]
            return result

    def test_accepts_direct_provider_response(self):
        payload = {"errno": 0, "list": [{"server_filename": "video.mp4"}]}

        self.assertEqual(self._metadata_from(payload), payload)
        self.assertEqual(
            self.proxy_payload["verification"]["trigger"],
            {
                "status_code": 403,
                "field": "code",
                "equals": "CHALLENGE_REQUIRED",
            },
        )
        self.assertEqual(
            self.proxy_payload["verification"]["site_key"],
            terabox_dl.TERABOXDL_TURNSTILE_SITE_KEY,
        )

    def test_accepts_legacy_nested_provider_response(self):
        payload = {"errno": 0, "list": [{"server_filename": "video.mp4"}]}
        proxy_response = {
            "time_taken": 1.2,
            "target_url_response": {"body": payload},
        }

        self.assertEqual(self._metadata_from(proxy_response), payload)

    def test_reports_provider_errno(self):
        with patch.object(terabox_dl, "_get_video_metadata", return_value={"errno": 2, "errmsg": "invalid link"}):
            with self.assertRaisesRegex(Exception, r"errno=2.*invalid link"):
                terabox_dl.get_video_info("https://terabox.com/s/1example", is_hd=False)

    def test_reports_proxy_error_message(self):
        with patch.object(
            terabox_dl,
            "_get_video_metadata",
            return_value={"success": False, "error": "Verification required"},
        ):
            with self.assertRaisesRegex(Exception, "Verification required"):
                terabox_dl.get_video_info("https://terabox.com/s/1example", is_hd=False)


if __name__ == "__main__":
    unittest.main()
