"""One-time interactive Tesla login. Run this yourself, directly on the Pi:

    ./venv/bin/python3 setup_tesla_auth.py

It prints a URL for you to open in any browser on any device. Log into the
real Tesla page there (your password goes to Tesla, never to this script).
After login, your browser will try to redirect to a "tesla://" address and
fail (a "page not found" or "open in app?" prompt, or similar) — that's
expected, since nothing's listening there. Copy that attempted URL from
your browser's address bar (or from its network/dev tools if your browser
hides it) and paste it back here.
"""

import tesla_auth
import tesla_api

AUTH_FILE = "tesla_auth.json"


def main() -> None:
    email = input("Tesla account email: ").strip()
    url, verifier, state = tesla_auth.build_authorize_url(email)

    print("\nOpen this URL in a browser and log in:\n")
    print(url)
    print(
        "\nAfter logging in, the page will fail to redirect (expected). "
        "Paste the full URL it tried to go to below.\n"
    )
    pasted = input("Redirect URL: ").strip()

    code = tesla_auth.parse_redirect(pasted)
    print("Exchanging code for tokens...")
    tokens = tesla_auth.exchange_code(code, verifier)
    tesla_auth.save_tokens(tokens, AUTH_FILE)
    print(f"Saved to {AUTH_FILE}")

    print("Looking up your energy site...")
    site_id = tesla_api.get_energy_site_id(tokens["access_token"])
    print(f"Found energy site ID: {site_id}")

    with open(".env", "a") as f:
        f.write(f"\nPW_SITE_ID={site_id}\n")
    print("Appended PW_SITE_ID to .env — setup complete.")


if __name__ == "__main__":
    main()
