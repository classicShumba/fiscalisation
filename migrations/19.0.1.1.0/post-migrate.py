"""Initialise automatic fiscalisation state.

Documents fiscalised before this version are marked 'done'. Everything else stays
'not_applicable' so the new queue never picks up historic (pre go-live) invoices;
they can still be fiscalised one by one with the manual button.
"""
import logging

from odoo import SUPERUSER_ID, api, fields

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE account_move
           SET fiscal_state = 'done'
         WHERE fiscalised IS TRUE
    """)
    _logger.info("fiscalisation: marked %s fiscalised documents as done", cr.rowcount)
    cr.execute("""
        UPDATE account_move
           SET fiscal_state = 'not_applicable'
         WHERE fiscal_state IS NULL
    """)
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.config_parameter'].set_param('fiscalisation.auto_since', fields.Datetime.to_string(fields.Datetime.now()))
