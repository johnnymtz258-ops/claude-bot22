import pytest

from fomo.swaps import USDC, WSOL, account_keys, parse_swap, trader_swap
from fomo.util import is_address
from tests.helpers import (LAMPORTS, ME, MINT, MINT2, POOL, RENT, SPONSOR, WHALE, WHALE2, build_tx, pump_buy,
                           pump_sell)


def test_fixture_addresses_are_valid():
    for a in (WHALE, WHALE2, ME, SPONSOR, MINT, MINT2, POOL):
        assert is_address(a), a


def test_buy_removes_fee_and_token_account_deposit():
    s = parse_swap(pump_buy(sol=1.5, tokens=3_000_000), WHALE)
    assert s["side"] == "BUY" and s["mint"] == MINT
    assert s["base"] == "SOL"
    assert s["base_amount"] == pytest.approx(1.5, abs=1e-9)     # not 1.5021 (deposit) or 1.5022 (fee)
    assert s["token_amount"] == pytest.approx(3_000_000)
    assert s["price_base"] == pytest.approx(5e-7)
    assert s["new_position"] is True
    assert s["fee_sol"] == pytest.approx(0.000105)
    assert s["deposit_sol"] == pytest.approx(RENT / LAMPORTS)
    assert s["dex"] == "Pump.fun"


def test_small_buy_is_not_distorted_by_deposit():
    # a $5 buy at $150/SOL: the 0.002 SOL deposit alone would look like a -9% loss
    s = parse_swap(pump_buy(sol=5 / 150, tokens=40_000), WHALE)
    assert s["base_amount"] == pytest.approx(5 / 150, rel=1e-6)


def test_full_sell_with_account_close_excludes_refund():
    s = parse_swap(pump_sell(sol=2.0, tokens=3_000_000, holding=3_000_000, close=True), WHALE)
    assert s["side"] == "SELL"
    assert s["base_amount"] == pytest.approx(2.0, abs=1e-9)
    assert s["sell_fraction"] == pytest.approx(1.0)
    assert s["holding_after"] == 0


def test_partial_sell_fraction_and_remaining():
    s = parse_swap(pump_sell(sol=1.0, tokens=750_000, holding=3_000_000), WHALE)
    assert s["sell_fraction"] == pytest.approx(0.25)
    assert s["holding_after"] == pytest.approx(2_250_000)
    assert s["pre_holding"] == pytest.approx(3_000_000)


def test_airdrop_is_not_a_buy():
    tx = build_tx(fee_payer=WHALE, wallet=WHALE, wallet_lamports_delta=-RENT - 5000,
                  tokens=[(WHALE, MINT, None, 1_000_000_000, 6)], create_ata_for=[(WHALE, MINT)])
    assert parse_swap(tx, WHALE) is None


def test_failed_transaction_is_ignored():
    tx = pump_buy()
    tx["meta"]["err"] = {"InstructionError": [2, {"Custom": 6001}]}
    assert parse_swap(tx, WHALE) is None


def test_token_to_token_swap_is_ignored():
    tx = build_tx(fee_payer=WHALE, wallet=WHALE, wallet_lamports_delta=-5000,
                  tokens=[(WHALE, MINT, 5_000_000, 0, 6), (WHALE, MINT2, 0, 9_000_000, 6)])
    assert parse_swap(tx, WHALE) is None


def test_usdc_buy():
    tx = build_tx(fee_payer=WHALE, wallet=WHALE, wallet_lamports_delta=-5000,
                  tokens=[(WHALE, USDC, 1_000_000_000, 750_000_000, 6), (WHALE, MINT, 0, 2_000_000_000, 6)])
    s = parse_swap(tx, WHALE)
    assert s["side"] == "BUY" and s["base"] == "USDC"
    assert s["base_amount"] == pytest.approx(250.0)
    assert s["token_amount"] == pytest.approx(2000)


def test_existing_wsol_account_counts_as_sol():
    tx = build_tx(fee_payer=WHALE, wallet=WHALE, wallet_lamports_delta=-5000,
                  tokens=[(WHALE, WSOL, 3 * LAMPORTS, 2 * LAMPORTS, 9), (WHALE, MINT, 0, 5_000_000, 6)])
    s = parse_swap(tx, WHALE)
    assert s["base"] == "SOL" and s["base_amount"] == pytest.approx(1.0)


def test_sponsored_transaction_where_app_pays_fee_and_deposit():
    # Fomo-style: the app's relayer pays network fee and the new token account deposit
    tx = build_tx(fee_payer=SPONSOR, wallet=ME, wallet_lamports_delta=-int(0.2 * LAMPORTS),
                  tokens=[(ME, MINT, None, 400_000_000_000, 6)], create_ata_for=[(ME, MINT, SPONSOR)])
    s = parse_swap(tx, ME)
    assert s["base_amount"] == pytest.approx(0.2)
    assert s["fee_sol"] == 0 and s["deposit_sol"] == 0


def test_dust_sol_movement_is_not_a_trade():
    tx = build_tx(fee_payer=WHALE, wallet=WHALE, wallet_lamports_delta=-100_000 - 5000,
                  tokens=[(WHALE, MINT, 1_000_000, 2_000_000, 6)])
    assert parse_swap(tx, WHALE) is None


def test_json_encoding_with_lookup_tables():
    tx = pump_buy()
    msg = tx["transaction"]["message"]
    keys = [k["pubkey"] for k in msg["accountKeys"]]
    # move everything after index 1 into a lookup table, like a v0 transaction in "json" encoding
    msg["accountKeys"] = keys[:2]
    msg["header"] = {"numRequiredSignatures": 1}
    tx["meta"]["loadedAddresses"] = {"writable": keys[2:], "readonly": []}
    assert account_keys(tx) == keys
    s = parse_swap(tx, WHALE)
    assert s and s["side"] == "BUY" and s["base_amount"] == pytest.approx(1.5)


def test_unparsed_instructions_fall_back_to_fee_payer_deposit():
    tx = build_tx(fee_payer=WHALE, wallet=WHALE, wallet_lamports_delta=-int(0.5 * LAMPORTS) - RENT - 5000,
                  tokens=[(WHALE, MINT, None, 1_000_000, 6)], parsed_instructions=False)
    assert parse_swap(tx, WHALE)["base_amount"] == pytest.approx(0.5)


def test_wallet_not_in_transaction():
    assert parse_swap(pump_buy(wallet=WHALE), WHALE2) is None


def test_trader_swap_finds_the_signer():
    who, s = trader_swap(pump_buy(wallet=WHALE2, sol=3.0), MINT)
    assert who == WHALE2 and s["base_amount"] == pytest.approx(3.0)
    assert trader_swap(pump_buy(wallet=WHALE2), MINT2) is None
