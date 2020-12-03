#!/usr/bin/python3

import csv
import datetime
import logging
import math
import sys

import tabula

import cbalib


CLOSING_PREFIX = "Closing balance at "

INTEREST_PREFIX = "Interest charged on "

RAW_TRAILERS = [
    "How to pay We’re here to help",
    "Please check your transactions listed on this statement and report any discrepancy to the Bank before the payment due date.",
]

TXN_HEADER = ["Date", "Transaction Details", "Amount (A$)"]

BALANCE_INFO_PREFIX_MAP = {
    "Opening balance at ": "opening",
    "New transactions and charges": "debits",
    "Payments/refunds": "credits",
    "Closing balance at ": "closing",
}

STATEMENT_HEADERS = ("Statement Period", "Statement period")


def parse_txns_from_df(df):
    header_found = False
    accum = []
    txns_raw = []

    def maybe_flush_frags():
        if accum != []:
            assert len(accum) <= 3
            txns_raw.append((accum[0][0], "\n".join(x[1] for x in accum), accum[0][2],))
            accum.clear()

    for i, row in df.iterrows():
        bits = [
            (x if not isinstance(x, float) or not math.isnan(x) else None) for x in row
        ]

        # Consume rows until we see the expected table header, as some
        # statement pages have stuff before the table that might be misparsed.
        if bits == TXN_HEADER:
            header_found = True
            continue
        if not header_found:
            continue
        # The last page of the statement contains extra bits which aren't rows.
        # Identify the misparsed final row by concatenating the cells and
        # comparing to the known string, and skip the rest of the rows.
        if all(isinstance(x, str) for x in bits) and "".join(bits).replace(" ", "") in [
            x.replace(" ", "") for x in RAW_TRAILERS
        ]:
            break

        # A fragment has a date and amount iff it's the first fragment of a
        # transaction. Subsequent fragments without a date or amount are part
        # of this transaction.
        # One exception: interest charges have a single fragment with the
        # implicit date of the statement's closing.
        if bits[0] is not None or bits[1].startswith(INTEREST_PREFIX):
            assert bits[2] is not None
            # If there were existing accumulated fragments, they are from a
            # previous transaction. Flush them.
            maybe_flush_frags()
        else:
            assert bits[2] is None
            assert accum != []
        accum.append(bits)

    maybe_flush_frags()

    txns = []
    for r in txns_raw:
        # Interest rows don't have an explicit date. The machine-readable
        # exports use the statement close date, which we'll fill in later.
        if r[1].startswith(INTEREST_PREFIX):
            d = None
        else:
            d = cbalib.parse_dd_mmm(r[0])
        desc = r[1]
        value = cbalib.parse_amount(r[2], allow_negative=True, negative_trailing=True)
        txns.append((d, desc, value))
    return txns


def validate_and_date_txns(
    txns, opening_balance, closing_balance, opening_year, closing_date
):
    """Process transactions to have real dates and validate them

    Includes checking that the transactions between the opening and closing
    lines sum to the difference in balance.
    """
    dated_txns = []
    last_year = opening_year
    last_month_day = None
    running_balance = opening_balance
    for (d, desc, value) in txns:
        if d is None and desc.startswith(INTEREST_PREFIX):
            d = closing_date
        month, day = d
        # Year has to be inferred from the opening line.
        if last_month_day is None:
            last_month_day = (month, day)

        # Year changes must be inferred from the month and day going backwards.
        if (month, day) < last_month_day:
            last_year += 1
        last_month_day = (month, day)

        running_balance -= value

        assert 2000 <= last_year <= 2100
        dated_txns.append(
            (datetime.date(last_year, month, day), desc, -value, running_balance)
        )

    # Just confirm we got to the end with a matching balance.
    assert closing_balance == running_balance

    return dated_txns


def parse_balance_info_from_df(df):
    info = {}
    for i, row in df.iterrows():
        bits = [
            (x if not isinstance(x, float) or not math.isnan(x) else None) for x in row
        ]
        key = None
        for prefix, prefix_key in BALANCE_INFO_PREFIX_MAP.items():
            if bits[0] is not None and bits[0].startswith(prefix):
                key = prefix_key
        if key is not None:
            info[key] = cbalib.parse_amount(bits[1], leading_dollar=True, allow_negative=True)
        if bits[0] is not None and bits[0].startswith(CLOSING_PREFIX):
            info["closing_date"] = cbalib.parse_dd_mmm(bits[0][len(CLOSING_PREFIX):])
    return info


def main(args):
    # Supress warnings like "Can't determine the width of the space character, assuming 250".
    tabula.io.logger.setLevel(logging.ERROR)

    # Parse the statement start year from the top right of the first page.
    [statement_df] = tabula.read_pdf(
        args[0],
        pages=1,
        area=[110, 348, 180, 573],
        columns=[440],
        pandas_options={"header": None},
    )
    statement_period_header, statement_period = list(statement_df.iterrows())[0][1]
    assert statement_period_header in STATEMENT_HEADERS
    opening_year = int(statement_period.split(" ")[2])
    assert 2000 <= opening_year <= 2100

    # Parse the opening/closing balance information from the left of the first page.
    [balance_df] = tabula.read_pdf(
        args[0],
        pages=1,
        area=[233, 0, 345, 297],
        columns=[200],
        pandas_options={"header": None},
    )

    # And parse the transactions.
    txn_dfs = tabula.read_pdf(
        args[0], pages="all", area=[94, 40, 803, 573], columns=[100, 500]
    )

    balance_info = parse_balance_info_from_df(balance_df)

    txns = []
    for df in txn_dfs:
        txns.extend(parse_txns_from_df(df))
    txns = validate_and_date_txns(
        txns,
        -balance_info["opening"],
        -balance_info["closing"],
        opening_year,
        balance_info["closing_date"],
    )

    csvwriter = csv.writer(sys.stdout)
    for date, desc, value, balance in txns:
        csvwriter.writerow(
            [date.isoformat(), " ".join(desc.splitlines()), value, balance]
        )


if __name__ == "__main__":
    main(sys.argv[1:])
