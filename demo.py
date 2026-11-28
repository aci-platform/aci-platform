"""ACI demo runner for screen recording.

Usage:
    python demo.py              # recording amounts: 85 (allow), 200 (decline), 130 (review)
    python demo.py 84 199 129   # rehearsal amounts, so you don't trigger the 60-second replay

Needs ACI_API_KEY and ACI_BASE_URL set in this Terminal window.
"""
import re
import sys

from aci_core import authorize_purchase, claim_approved_card

MERCHANT, CATEGORY = "amazon.co.uk", "toner"


def title(text):
    print(f"\n{'=' * 60}\n  {text}\n{'=' * 60}")


def wait(msg):
    input(f"\n  >> {msg} (press Enter) ")


def main():
    allow_amt, decline_amt, review_amt = (float(a) for a in (sys.argv[1:4] or [85, 200, 130]))

    title("1. The agent asks to spend within its mandate")
    print(f"  authorize_purchase({allow_amt:g}, '{MERCHANT}', '{CATEGORY}')\n")
    print(" ", authorize_purchase(allow_amt, MERCHANT, CATEGORY))

    wait("Next: a purchase over the transaction limit")
    title("2. The agent asks to spend too much")
    print(f"  authorize_purchase({decline_amt:g}, '{MERCHANT}', '{CATEGORY}')\n")
    print(" ", authorize_purchase(decline_amt, MERCHANT, CATEGORY))

    wait("Next: a purchase that needs a human")
    title("3. A purchase that needs human approval")
    print(f"  authorize_purchase({review_amt:g}, '{MERCHANT}', '{CATEGORY}')\n")
    result = authorize_purchase(review_amt, MERCHANT, CATEGORY)
    print(" ", result)
    match = re.search(r"dec_[0-9a-f]+", result)
    if not result.startswith("REVIEW") or not match:
        print("\n  Unexpected result, stop and check the agent's settings.")
        return

    wait("Now approve it in the dashboard, then come back here")
    title("4. The agent claims its single-use card")
    print(f"  claim_approved_card('{match.group(0)}')\n")
    print(" ", claim_approved_card(match.group(0)))
    print()


if __name__ == "__main__":
    main()
