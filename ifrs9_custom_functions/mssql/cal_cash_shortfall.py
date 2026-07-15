"""
Brain Nexus Solutions
Calculate expected cash flow PV, 12m PV, cash shortfall and shortfall PV on
fsi_financial_cash_flow_cal in a single set-based SQL update for the given
fic_mis_date and latest run_skey.
"""
from django.db import connection, transaction

from IFRS9.models import Dim_Run
from .save_log import save_log


def get_latest_run_skey():
    """Retrieve the latest_run_skey from Dim_Run table."""
    try:
        run_record = Dim_Run.objects.only('latest_run_skey').first()
        if not run_record:
            save_log('get_latest_run_skey', 'ERROR', "No run key available.")
            return None
        return run_record.latest_run_skey
    except Exception as e:
        save_log('get_latest_run_skey', 'ERROR', str(e))
        return None


def cal_cash_shortfall(fic_mis_date):
    """
    Calculates and updates cash flow fields (expected cash flow PV, 12m PV,
    cash shortfall, cash shortfall PV, 12m shortfall, 12m shortfall PV) in
    fsi_financial_cash_flow_cal in a single set-based SQL update.
    Returns 1 if any row was updated, 0 otherwise.
    """
    run_skey = get_latest_run_skey()
    if not run_skey:
        save_log('cal_cash_shortfall', 'ERROR', "No valid run key found.", status='FAILURE')
        return 0

    try:
        save_log(
            'cal_cash_shortfall',
            'INFO',
            f"Starting cash shortfall update | fic_mis_date={fic_mis_date} | run_skey={run_skey} | "
            f"columns: n_expected_cash_flow_pv, n_12m_exp_cash_flow_pv, n_cash_shortfall, n_12m_cash_shortfall, n_cash_shortfall_pv, n_12m_cash_shortfall_pv.",
            status='SUCCESS',
        )

        with transaction.atomic(), connection.cursor() as cursor:
            # Pre-check: how many rows have inputs needed to compute at least one output
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM fsi_financial_cash_flow_cal
                WHERE fic_mis_date = %s AND n_run_skey = %s
                  AND (
                      (n_discount_factor IS NOT NULL AND (n_expected_cash_flow IS NOT NULL OR n_12m_exp_cash_flow IS NOT NULL))
                      OR (n_cash_flow_amount IS NOT NULL AND (n_expected_cash_flow IS NOT NULL OR n_12m_exp_cash_flow IS NOT NULL))
                  );
                """,
                [fic_mis_date, run_skey],
            )
            rows_with_inputs = (cursor.fetchone() or [0])[0]
            cursor.execute(
                "SELECT COUNT(*) FROM fsi_financial_cash_flow_cal WHERE fic_mis_date = %s AND n_run_skey = %s",
                [fic_mis_date, run_skey],
            )
            total_rows = (cursor.fetchone() or [0])[0]

            # Diagnostic: why some rows are not updated (missing inputs)
            cursor.execute(
                """
                SELECT
                    COUNT(CASE WHEN n_discount_factor IS NULL THEN 1 END),
                    COUNT(CASE WHEN n_expected_cash_flow IS NULL AND n_12m_exp_cash_flow IS NULL THEN 1 END),
                    COUNT(CASE WHEN n_cash_flow_amount IS NULL THEN 1 END)
                FROM fsi_financial_cash_flow_cal
                WHERE fic_mis_date = %s AND n_run_skey = %s
                """,
                [fic_mis_date, run_skey],
            )
            diag = cursor.fetchone()
            missing_discount = diag[0] if diag else 0
            missing_expected = diag[1] if diag else 0
            missing_cash_amt = diag[2] if diag else 0

            save_log(
                'cal_cash_shortfall',
                'INFO',
                f"Pre-check | fic_mis_date={fic_mis_date} | run_skey={run_skey} | "
                f"rows with required inputs: {rows_with_inputs} | total rows: {total_rows} | "
                f"rows not updated (missing inputs): {total_rows - rows_with_inputs} "
                f"(missing n_discount_factor: {missing_discount}, missing both expected_flow: {missing_expected}, missing n_cash_flow_amount: {missing_cash_amt}).",
                status='SUCCESS',
            )

            # Only update rows that have inputs to compute at least one field (so rowcount reflects actual updates)
            sql = """
                UPDATE fsi_financial_cash_flow_cal
                SET
                    n_expected_cash_flow_pv = CASE
                        WHEN n_discount_factor IS NOT NULL AND n_expected_cash_flow IS NOT NULL
                        THEN n_discount_factor * n_expected_cash_flow
                        ELSE n_expected_cash_flow_pv END,
                    n_12m_exp_cash_flow_pv = CASE
                        WHEN n_discount_factor IS NOT NULL AND n_12m_exp_cash_flow IS NOT NULL
                        THEN n_discount_factor * n_12m_exp_cash_flow
                        ELSE n_12m_exp_cash_flow_pv END,
                    n_cash_shortfall = CASE
                        WHEN n_cash_flow_amount IS NOT NULL AND n_expected_cash_flow IS NOT NULL
                        THEN (n_cash_flow_amount - n_expected_cash_flow)
                        ELSE n_cash_shortfall END,
                    n_12m_cash_shortfall = CASE
                        WHEN n_cash_flow_amount IS NOT NULL AND n_12m_exp_cash_flow IS NOT NULL
                        THEN (n_cash_flow_amount - n_12m_exp_cash_flow)
                        ELSE n_12m_cash_shortfall END,
                    n_cash_shortfall_pv = CASE
                        WHEN n_discount_factor IS NOT NULL AND n_cash_flow_amount IS NOT NULL AND n_expected_cash_flow IS NOT NULL
                        THEN n_discount_factor * (n_cash_flow_amount - n_expected_cash_flow)
                        ELSE n_cash_shortfall_pv END,
                    n_12m_cash_shortfall_pv = CASE
                        WHEN n_discount_factor IS NOT NULL AND n_cash_flow_amount IS NOT NULL AND n_12m_exp_cash_flow IS NOT NULL
                        THEN n_discount_factor * (n_cash_flow_amount - n_12m_exp_cash_flow)
                        ELSE n_12m_cash_shortfall_pv END
                WHERE fic_mis_date = %s AND n_run_skey = %s
                 ;
            """
            cursor.execute(sql, [fic_mis_date, run_skey])
            updated_count = cursor.rowcount

            # Verification: how many rows have each output set
            cursor.execute(
                """
                SELECT
                    COUNT(CASE WHEN n_expected_cash_flow_pv IS NOT NULL THEN 1 END),
                    COUNT(CASE WHEN n_12m_exp_cash_flow_pv IS NOT NULL THEN 1 END),
                    COUNT(CASE WHEN n_cash_shortfall IS NOT NULL THEN 1 END),
                    COUNT(CASE WHEN n_12m_cash_shortfall IS NOT NULL THEN 1 END),
                    COUNT(CASE WHEN n_cash_shortfall_pv IS NOT NULL THEN 1 END),
                    COUNT(CASE WHEN n_12m_cash_shortfall_pv IS NOT NULL THEN 1 END),
                    COUNT(*)
                FROM fsi_financial_cash_flow_cal
                WHERE fic_mis_date = %s AND n_run_skey = %s
                """,
                [fic_mis_date, run_skey],
            )
            row = cursor.fetchone()
            with_ecf_pv = row[0] if row else 0
            with_12m_ecf_pv = row[1] if row else 0
            with_shortfall = row[2] if row else 0
            with_12m_shortfall = row[3] if row else 0
            with_shortfall_pv = row[4] if row else 0
            with_12m_shortfall_pv = row[5] if row else 0
            total = row[6] if row else 0

        if updated_count > 0:
            not_updated = total - updated_count
            msg = (
                f"Cash shortfall updated | fic_mis_date={fic_mis_date} | run_skey={run_skey} | "
                f"rows updated: {updated_count} | verification: n_expected_cash_flow_pv={with_ecf_pv}, n_12m_exp_cash_flow_pv={with_12m_ecf_pv}, "
                f"n_cash_shortfall={with_shortfall}, n_12m_cash_shortfall={with_12m_shortfall}, n_cash_shortfall_pv={with_shortfall_pv}, n_12m_cash_shortfall_pv={with_12m_shortfall_pv} (total rows: {total})."
            )
            if not_updated > 0:
                msg += f" {not_updated} rows not updated (missing n_discount_factor and/or n_expected_cash_flow/n_12m_exp_cash_flow and/or n_cash_flow_amount — run earlier pipeline steps to populate these)."
            save_log(
                'cal_cash_shortfall',
                'INFO',
                msg,
                status='SUCCESS',
            )
        else:
            save_log(
                'cal_cash_shortfall',
                'WARNING',
                f"No rows updated | fic_mis_date={fic_mis_date} | run_skey={run_skey} | "
                f"total rows: {total} | rows with required inputs: {rows_with_inputs} | "
                f"verification: n_expected_cash_flow_pv={with_ecf_pv}, n_cash_shortfall={with_shortfall}. "
                f"Populate n_discount_factor (run discount factor step), n_expected_cash_flow or n_12m_exp_cash_flow, and n_cash_flow_amount on fsi_financial_cash_flow_cal for this date+run.",
                status='SUCCESS',
            )
        return 1 if updated_count > 0 else 0

    except Exception as e:
        save_log(
            'cal_cash_shortfall',
            'ERROR',
            f"Error: {e}",
            status='FAILURE',
        )
        return 0