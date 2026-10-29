"""Shared test helper: fund a deal the way a user does (closing record + typed funding confirmation)."""

from datetime import date


def fund_deal(conn, deal_id, amount="480000", by="Pat Broker",
              override="Test scenario: no accepted offer or open items are tracked here"):
    from uwhelper.origination.closing import (
        FUNDING_PHRASE,
        ClosingIn,
        FundingConfirmIn,
        confirm_funding,
        save_closing,
    )

    save_closing(conn, deal_id, ClosingIn(closing_date=date.today()))
    return confirm_funding(conn, deal_id, FundingConfirmIn(funded_amount=amount, funding_date=date.today(),
                                                           confirmed_by=by, confirmation_text=FUNDING_PHRASE,
                                                           override_reason=override))
