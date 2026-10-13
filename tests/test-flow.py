"""
POST : 
https://diskwala.141.148.151.172.sslip.io/v2/video

x-api-key : 760d199dac79f5447ae61a5354ed37f9

{ "url": "https://www.diskwala.com/app/6a610deb06ba7ea03d7ad619"}

response:
{
    "method": "POST",
    "url": "https://api.diskwala.com/api/v2/android/np_lelo",
    "req_headers": {
        "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1aWQiOiI2YWM5ZTA5MDgwZGMwZjg5N2Q2NmEzNmMiLCJkaWQiOiIxYzMzNzNjZTBiNjBlNWIzIiwicGlkIjoiZGV2aWNlOjFjMzM3M2NlMGI2MGU1YjMiLCJwcnYiOiJkZXZpY2UiLCJwbHQiOiJhbmRyb2lkIiwidGllciI6ImJhc2ljIiwiZ3YiOnRydWUsImFjdCI6IlVORVZBTFVBVEVEIiwiaWF0IjoxNzkxNjk5MTc4LCJleHAiOjE3OTE3MDI3Nzh9.hXe-9TNJax8f42ZgxJJwyF02HjMNRNEQ37dq-OyHYEc",
        "x-dw-kid": "2XXgjQqheEZoG8KHc1KQDzqUy0MP8q7LM1NV5AkTHuQ",
        "x-dw-ts": "1791700452291",
        "x-dw-nonce": "9cWzdmClwKou7k7qZAAOAw",
        "x-dw-sig": "MEYCIQCDE4joPrrzk2kSqcDVynrS99oW2TyZ86F1ffrb1bPtfAIhALbIhrudegc8t/0SuizIL5ku7arZTkW4g9mEKUSoyXGB",
        "content-type": "application/json; charset=utf-8",
        "content-length": "118",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/4.12.0"
    },
    "req_body": "{\"id\":\"6a610deb06ba7ea03d7ad619\",\"t\":\"2026-10-11T06:34:12.177Z\",\"c_i\":{},\"user\":{},\"s\":false,\"dID\":\"1c3373ce0b60e5b3\"}"
}

====
USE ABOVE SCHEMA TO CALL ACTUAL SERVER

RESPONSE :

{
  "fileInfo": "U2FsdGVkX1+abc123ExampleEncryptedValue...==",
  "uploader": {
    "name": "Example"
  }
}


====

POST:

x-api-key : 760d199dac79f5447ae61a5354ed37f9
https://diskwala.141.148.151.172.sslip.io/decrypt 
{
  "encrypted_body":  "PREV JSON RESPONSE"
}

************ FINAL OUTPUT ****************

====
Exercises the diskwala proxy flow:

1. POST /v2/video with a diskwala share URL -> returns an encrypted "fileInfo" blob.
2. POST /decrypt with that blob -> returns the decrypted JSON payload.

"""
import sys

import requests

BASE_URL = "https://diskwala.141.148.151.172.sslip.io"
API_KEY = "760d199dac79f5447ae61a5354ed37f9"
VIDEO_URL = "https://www.diskwala.com/app/6a610deb06ba7ea03d7ad619"
TIMEOUT = 30


def sign_request(video_url: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/v2/video",
        headers={"x-api-key": API_KEY},
        json={"url": video_url},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def send_upstream(signed: dict) -> dict:
    resp = requests.request(
        signed["method"],
        signed["url"],
        headers=signed["req_headers"],
        data=signed["req_body"].encode("utf-8"),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def decrypt(upstream_resp: dict) -> dict:
    resp = requests.post(
        f"{BASE_URL}/decrypt",
        headers={"x-api-key": API_KEY},
        json={"encrypted_body": upstream_resp},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        print(f"decrypt failed {resp.status_code}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    video_url = sys.argv[1] if len(sys.argv) > 1 else VIDEO_URL

    print(f"[1/3] POST {BASE_URL}/v2/video  url={video_url}")
    signed = sign_request(video_url)
    print(signed)

    print(f"\n[2/3] {signed['method']} {signed['url']} (upstream)")
    upstream_resp = send_upstream(signed)
    print(upstream_resp)

    if "fileInfo" not in upstream_resp:
        print("No 'fileInfo' field in upstream response, cannot decrypt", file=sys.stderr)
        return 1

    print(f"\n[3/3] POST {BASE_URL}/decrypt")
    decrypted = decrypt(upstream_resp)
    print(decrypted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
