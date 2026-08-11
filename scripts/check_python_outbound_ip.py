#!/usr/bin/env python3
import argparse
import os
import socket
import sys
import urllib.request

import requests


IP_ENDPOINTS = [
    ("ipify", "https://api.ipify.org?format=json"),
    ("ifconfig.me", "https://ifconfig.me/ip"),
    ("icanhazip", "https://icanhazip.com"),
]

PROXY_ENV_NAMES = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
]

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


def mask_proxy(value):
    if not value:
        return ""
    if "@" not in value:
        return value
    scheme, rest = value.split("://", 1) if "://" in value else ("", value)
    _, host = rest.rsplit("@", 1)
    return f"{scheme + '://' if scheme else ''}***:***@{host}"


def print_environment():
    print("Python executable:", sys.executable)
    print("Hostname:", socket.gethostname())
    print()
    print("Proxy environment variables:")
    found = False
    for name in PROXY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            found = True
            print(f"  {name}={mask_proxy(value)}")
    if not found:
        print("  none")
    print()

    env_proxies = requests.utils.get_environ_proxies(GMAIL_SEND_URL)
    print("requests proxies resolved for Gmail API URL:")
    if env_proxies:
        for key, value in env_proxies.items():
            print(f"  {key}: {mask_proxy(value)}")
    else:
        print("  none")
    print()


def fetch_with_requests(name, url, timeout, proxies):
    response = requests.get(url, timeout=timeout, proxies=proxies)
    response.raise_for_status()
    text = response.text.strip()
    try:
        data = response.json()
        text = data.get("ip") or data.get("origin") or text
    except ValueError:
        pass
    print(f"requests / {name}: {text}")


def fetch_with_urllib(name, url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace").strip()
    print(f"urllib   / {name}: {text}")


def main():
    parser = argparse.ArgumentParser(
        description="Show the public outbound IP used by this Python process."
    )
    parser.add_argument(
        "--proxy",
        help="Optional explicit proxy, for example http://127.0.0.1:7890 or socks5h://127.0.0.1:1080.",
    )
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args()

    print_environment()

    proxies = None
    if args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}
        print(f"Explicit proxy for this test: {mask_proxy(args.proxy)}")
        print()

    had_success = False
    for name, url in IP_ENDPOINTS:
        try:
            fetch_with_requests(name, url, args.timeout, proxies)
            had_success = True
            break
        except Exception as exc:
            print(f"requests / {name}: failed: {exc}")

    print()
    for name, url in IP_ENDPOINTS[:1]:
        try:
            fetch_with_urllib(name, url, args.timeout)
            had_success = True
        except Exception as exc:
            print(f"urllib   / {name}: failed: {exc}")

    if not had_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
