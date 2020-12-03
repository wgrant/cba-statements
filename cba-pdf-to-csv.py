#!/usr/bin/python3

import csv
import datetime
import decimal
import logging
import math
import sys

import tabula

import cbalib

RAW_TRAILER = "Opening balance - Total debits + Total credits = Closing balance"

SPECIAL_LINE_MAP = {
    "CREDIT INTEREST EARNED on this account": 2,
    "BONUS INTEREST EARNED on this account to": 2,
}

BALANCE_ONLY_SUFFIXES = (" OPENING BALANCE", "CLOSING BALANCE")


def is_valid_credit_prefix(c):
    # New statements are consistently $, but values seen in old statements
    # include -, $, 3 and 4.
    return len(c) == 1 and (c in ("-", "$") or c.isdigit())


def parse_txns_from_df(df):
    header_found = False
    accum = []
    txns_raw = []
    special_lines = None

    for i, row in df.iterrows():
        bits = [
            (x if not isinstance(x, float) or not math.isnan(x) else None) for x in row
        ]

        # Old-style (e.g. 2012) statements start and end each page with a weird
        # balance line without a date. Skip those.
        if all(isinstance(x, str) for x in bits[2:]) and "".join(bits[2:]).replace(
            " ", ""
        ).startswith("BALANCECARRIEDFORWARD"):
            continue
        if isinstance(bits[1], str) and bits[1].startswith("BALANCE BROUGHT FORWARD"):
            continue

        # Consume rows until we see the expected table header, as some
        # statement pages have stuff before the table that might be misparsed.
        if bits == ["Date", "Transaction", "Debit", "Credit", "Balance"]:
            header_found = True
            continue
        if not header_found:
            continue
        # The last page of the statement contains extra bits which aren't rows.
        # Identify the misparsed final row by concatenating the cells and
        # comparing to the known string, and skip the rest of the rows.
        if (
            bits[0] is None
            and all(isinstance(x, str) for x in bits[1:])
            and "".join(bits[1:]).replace(" ", "") == RAW_TRAILER.replace(" ", "")
        ):
            break

        # The opening/closing lines merge the date and description columns.
        # They start with day/month in a way that looks almost identical to
        # normal rows, but the alignment of the divider can be slightly off
        # depending on the width of the day/month, sometimes causing it to
        # split after the first character of the description. If so, resplit
        # them here.
        if (
            bits[1].endswith(BALANCE_ONLY_SUFFIXES)
            and len(bits[0]) == 8
            and bits[0].endswith("2")
        ):
            bits[0] = bits[0][:6]
            bits[1] = "2" + bits[1]

        # A fragment has a date iff it's the first fragment of a transaction.
        if bits[0] is not None:
            assert accum == []
        else:
            assert accum != []
        accum.append(bits)

        # Some rows are special and are split into multiple lines without a
        # balance.
        special_lines = SPECIAL_LINE_MAP.get(bits[1], None) or special_lines

        # Only the final fragment of a transaction has a balance, and it also
        # has either a debit or credit unless the previous fragment is known to
        # be special.
        if special_lines:
            special_lines -= 1
        elif bits[4] is None:
            if bits[2:] != [None, None, None]:
                raise AssertionError(
                    "Line without balance but with another value: {}".format(bits)
                )
            continue

        # If this is a known fixed-length thing, await more lines.
        if special_lines:
            continue

        assert len(accum) <= 3
        txns_raw.append(
            (
                accum[0][0],
                "\n".join(x[1] for x in accum),
                accum[-1][2],
                accum[-1][3],
                accum[-1][4],
            )
        )
        accum = []
    assert accum == []

    txns = []
    for r in txns_raw:
        d = cbalib.parse_dd_mmm(r[0])
        desc = r[1]
        value = None
        no_balance = False
        if desc.endswith(BALANCE_ONLY_SUFFIXES):
            assert r[2] is None and r[3] is None
        elif desc.startswith(tuple(SPECIAL_LINE_MAP.keys())):
            assert r[2] is None and is_valid_credit_prefix(r[3]) and r[4] is None
            no_balance = True
        elif r[2] is not None:
            # Debits have an invisible character in the Credit column.
            assert is_valid_credit_prefix(r[3])
            value = -cbalib.parse_amount(r[2])
        else:
            # Credits start with an invisible character.
            assert is_valid_credit_prefix(r[3][0])
            value = cbalib.parse_amount(r[3][1:])
        if no_balance:
            assert r[4] is None
            balance = None
        else:
            if r[4] in ("Nil", "$0.00"):
                balance = decimal.Decimal("0.00")
            else:
                balance = cbalib.parse_amount(r[4], leading_dollar=True, cr_dr=True)
        txns.append((d, desc, value, balance))
    return txns


def validate_and_date_txns(txns):
    """Process transactions to have real dates and validate them

    Includes checking that the transactions between the opening and closing
    lines sum to the difference in balance.
    """
    dated_txns = []
    last_year = None
    last_month_day = None
    found_closing = False
    running_balance = None
    for ((month, day), desc, value, balance) in txns:
        # Year has to be inferred from the opening line.
        if desc.endswith(" OPENING BALANCE"):
            assert last_year is None
            last_year = int(desc[: -len(" OPENING BALANCE")])
            last_month_day = (month, day)
            assert value is None
            running_balance = balance
        else:
            assert last_year is not None

        # Year changes must be inferred from the month and day going backwards.
        if (month, day) < last_month_day:
            last_year += 1
        last_month_day = (month, day)

        # Check that the running balance total matches, ensuring we haven't missed
        # any transactions.
        if value is not None:
            running_balance += value
        if balance is not None and balance != running_balance:
            raise AssertionError(
                "Balance mismatch: running {} != imported {}".format(
                    running_balance, balance
                )
            )

        if desc.endswith(" CLOSING BALANCE"):
            assert int(desc[: -len(" CLOSING BALANCE")]) == last_year
            assert value is None
            assert balance == running_balance
            found_closing = True

        assert 2000 <= last_year <= 2100
        dated_txns.append((datetime.date(last_year, month, day), desc, value, balance))

    # Just confirm we got to the end with a matching balance.
    assert found_closing

    return dated_txns


def main(args):
    # Supress warnings like "Can't determine the width of the space character, assuming 250".
    tabula.io.logger.setLevel(logging.ERROR)

    dfs = tabula.read_pdf(
        args[0],
        multiple_tables=True,
        pages="all",
        area=[48, 55, 800, 542],
        # 82.6 is the magic value that correctly splits even the
        # opening/closing lines which technically don't have separate cells for
        # the day/month and year... except for my last few Complete Access
        # statements. TODO.
        columns=[86, 320, 390, 470],
    )

    txns = []
    for df in dfs:
        txns.extend(parse_txns_from_df(df))
    txns = validate_and_date_txns(txns)

    csvwriter = csv.writer(sys.stdout)
    for date, desc, value, balance in txns:
        csvwriter.writerow(
            [date.isoformat(), " ".join(desc.splitlines()), value, balance]
        )


if __name__ == "__main__":
    main(sys.argv[1:])
