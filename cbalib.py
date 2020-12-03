import calendar
import decimal


def parse_dd_mmm(s):
    raw_day, raw_month = s.split(" ")
    assert raw_day.isdigit()
    assert len(raw_day) == 2
    day = int(raw_day)
    month = list(calendar.month_abbr).index(raw_month)
    return (month, day)


def parse_amount(
    s, leading_dollar=False, allow_negative=False, negative_trailing=False, cr_dr=False
):
    assert not allow_negative or not cr_dr
    assert allow_negative or not negative_trailing
    mul = 1

    # Handle trailing CR/DR.
    if cr_dr:
        if s.endswith(" DR"):
            mul = -1
            s = s[:-3]
        elif s.endswith(" CR"):
            s = s[:-3]
        else:
            raise AssertionError()

    # Handle leading or trailing negative as specified.
    if allow_negative:
        if negative_trailing:
            if s[-1] == "-":
                s = s[:-1]
                mul = -1
        else:
            if s[0] == "-":
                s = s[1:]
                mul = -1

    # Handle and enforce a leading dollar sign.
    if leading_dollar:
        assert s[0] == "$"
        s = s[1:]

    return mul * decimal.Decimal(s.replace(",", ""))
