from odoo import models, fields, api
from odoo.exceptions import UserError
import requests
from datetime import datetime, timedelta
import logging
import json
import re
from urllib.parse import urlencode

TIMEOUT = 15 

_logger = logging.getLogger(__name__)

class FiscalDevice(models.Model):
    _name = 'fiscal.device'
    _description = 'Fiscal Device'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    
    # Existing fields
    name = fields.Char(string='Device Name', required=True, copy=False, tracking=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, tracking=True, copy=False)
    device_id = fields.Integer(string='Device ID', required=True, tracking=True)
    device_serial = fields.Char(string='Device Serial', required=True, tracking=True)
    activation_key = fields.Char(string='Activation Key', required=True, tracking=True)
    base_url = fields.Char(string='Fisc API Base URL', default='https://fisc.classicshumba.co.zw', required=True, tracking=True)
    fdms_url = fields.Char(string='FDMS API Base URL', compute='_compute_fdms_url', required=True, tracking=True)
    access_token = fields.Char(string='Access Token', copy=False)
    refresh_token = fields.Char(string='Refresh Token', copy=False)
    token_expiry = fields.Datetime(string='Token Expiry')
    is_day_open = fields.Boolean(string='Day Open', compute="_compute_is_day_open", store=True, tracking=True)
    fiscal_day_status = fields.Char(string='Fiscal Day Status', readonly=True)
    last_receipt_global_no = fields.Integer(string='Last Global Receipt No', readonly=True)
    last_receipt_no = fields.Integer(string='Last Receipt No', readonly=True)
    fiscal_day_counters = fields.Json(string='Day Counters', readonly=True)
    fiscal_day_no = fields.Char(string='Fiscal Day No', readonly=True, copy=False)
    last_operation = fields.Datetime(string='Last Operation', readonly=True, copy=False)
    last_status_check = fields.Datetime(string='Last Status Check', readonly=True)
    
    # New error tracking fields
    last_error_code = fields.Char(string='Last Error Code', readonly=True)
    last_error_message = fields.Text(string='Last Error Message', readonly=True)
    last_error_status = fields.Integer(string='Last HTTP Status', readonly=True)

    # Notification configuration
    notification_email = fields.Char(
        string='Notification Email',
        default='dev@classicshumba.co.zw',
        help='Email address to receive fiscal device error notifications. Leave empty to use company users.',
        readonly=True
    )

    alert_user_ids = fields.Many2many(
        'res.users', 'fiscal_device_alert_user_rel', 'device_id', 'user_id',
        string='Notify on Failure',
        domain=[('share', '=', False)],
        help="Users who get a to-do when a document fails, is blocked or needs checking. "
             "Leave empty to notify all Fiscalisation Administrators.",
        tracking=True,
    )

    _unique_fisc_device = models.Constraint(
        'UNIQUE(company_id, device_id)',
        'Device ID must be unique per company!'
    )
    
    @api.depends('fiscal_day_status')
    def _compute_is_day_open(self):
        """Compute day open status based on fiscal day status"""
        for record in self:
            record.is_day_open = (record.fiscal_day_status or '').upper() != 'FISCALDAYCLOSED'

    @api.depends('base_url')
    def _compute_fdms_url(self):
        """FDMS URL for verification"""
        for record in self:
            if record.base_url in ['https://fisc.gethost.co.zw','https://fiscal.terralith.co.zw']:
                record.fdms_url = 'https://fdms.zimra.co.zw'
            else:
                record.fdms_url = 'https://fdmstest.zimra.co.zw'

    def _get_notification_recipients(self):
        """
        Get partner IDs for error notifications.
        Uses notification_email if set, otherwise falls back to company users.
        """
        self.ensure_one()

        if self.notification_email:
            # Find or create partner with the notification email
            partner = self.env['res.partner'].sudo().search([
                ('email', '=', self.notification_email)
            ], limit=1)

            if not partner:
                # Create a new partner for this notification email
                partner = self.env['res.partner'].sudo().create({
                    'name': 'Fiscal Device Notification',
                    'email': self.notification_email,
                    'company_id': self.company_id.id,
                    'type': 'contact',
                })
                _logger.info("Created notification partner for email: %s", self.notification_email)

            return partner.ids
        else:
            # Fallback to company users
            return self.company_id.user_ids.partner_id.ids

    def action_manual_token_refresh(self):
        """Refresh the access token from the form button."""
        self.ensure_one()
        try:
            self._get_new_token()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': self.env._("Token refreshed"),
                    'message': self.env._("The device has a new access token."),
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': self.env._("Token refresh failed"),
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def _refresh_token_if_needed(self):
        """Check and refresh token if expired"""
        self.ensure_one()
        if not self.token_expiry or datetime.now() > fields.Datetime.from_string(self.token_expiry):
            self._get_new_token()

    def _get_new_token(self):
        """Request a new access token from the fiscal API."""
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/devices/token",
                json={"device_serial": self.device_serial, "activation_key": self.activation_key},
                timeout=TIMEOUT
            )
            response.raise_for_status()
            token_data = response.json()
            
            if not all(k in token_data for k in ('access_token', 'refresh_token', 'expires_in')):
                raise UserError(self.env._("The token response from %s is missing fields.", self.base_url))
            
            self.write({
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'token_expiry': fields.Datetime.to_string(datetime.now() + timedelta(seconds=token_data['expires_in']))
            })
        except requests.exceptions.ConnectionError as e:
            error_msg = self.env._("Cannot connect to the fiscal API at %s.", self.base_url)
            self.write({
                'last_error_code': 'CONNECTION_REFUSED',
                'last_error_message': error_msg,
                'last_error_status': 0,
                'last_status_check': fields.Datetime.now(),
            })
            raise UserError(error_msg) from e
        except requests.exceptions.Timeout as e:
            error_msg = self.env._("The fiscal API did not answer the token request within %s seconds.", TIMEOUT)
            self.write({
                'last_error_code': 'TIMEOUT',
                'last_error_message': error_msg,
                'last_error_status': 0,
                'last_status_check': fields.Datetime.now(),
            })
            raise UserError(error_msg) from e
        except requests.exceptions.HTTPError as e:
            error_data = self._parse_error_response(e)
            self._log_error_details(error_data)
            raise UserError(self._format_error_message(error_data)) from e
        except Exception as e:
            error_msg = self.env._("Token refresh failed: %s", e)
            self.write({
                'last_error_code': 'UNKNOWN_ERROR',
                'last_error_message': error_msg,
                'last_error_status': 0,
                'last_status_check': fields.Datetime.now(),
            })
            raise UserError(error_msg) from e

    def _api_request(self, endpoint, method='POST', payload=None):
        """Call the fiscal API and raise a UserError with FDMS details on failure."""
        self.ensure_one()
        
        try:
            # Try to refresh token if needed, but handle connection errors here too
            try:
                self._refresh_token_if_needed()
            except requests.exceptions.ConnectionError as conn_err:
                error_data = {
                    'code': 'CONNECTION_REFUSED',
                    'message': self.env._("Cannot connect to the fiscal API at %s.", self.base_url),
                    'operation_id': '',
                    'status': 0
                }
                self._log_error_details(error_data)
                raise UserError(error_data['message']) from conn_err
                
            _logger.debug("API Request: %s %s", method, endpoint)
            response = requests.request(
                method,
                f"{self.base_url}{endpoint}",
                headers=self._get_auth_headers(),
                json=payload,
                timeout=TIMEOUT
            )
            response.raise_for_status()
            return response.json()
    
        except requests.exceptions.HTTPError as e:
            error_data = self._parse_error_response(e)
            self._log_error_details(error_data, e.response.json() if e.response else None)
            raise UserError(self._format_error_message(error_data)) from e
            
        except requests.exceptions.ConnectionError as e:
            error_data = {
                'code': 'CONNECTION_REFUSED',
                'message': self.env._("Cannot connect to the fiscal API at %s. Check that the server is running.", self.base_url),
                'operation_id': '',
                'status': 0
            }
            self._log_error_details(error_data)
            raise UserError(error_data['message']) from e
            
        except requests.exceptions.Timeout as e:
            error_data = {
                'code': 'TIMEOUT',
                'message': self.env._("The fiscal API did not answer within %s seconds.", TIMEOUT),
                'operation_id': '',
                'status': 0
            }
            self._log_error_details(error_data)
            raise UserError(error_data['message']) from e
            
        except requests.exceptions.RequestException as e:
            error_data = {
                'code': 'CONNECTION_FAILED',
                'message': self.env._("Connection to the fiscal API failed: %s", e),
                'operation_id': '',
                'status': 0
            }
            self._log_error_details(error_data)
            raise UserError(error_data['message']) from e

    def _parse_error_response(self, http_error):
        """Parse FDMS error response structure"""
        error_data = {
            'code': 'UNKNOWN',
            'message': self.env._("Unknown error"),
            'operation_id': '',
            'status': http_error.response.status_code
        }

        try:
            response_data = http_error.response.json()
            # Handle FDMSProblemDetails structure
            error_data.update({
                'code': response_data.get('errorCode', response_data.get('type', 'UNKNOWN')),
                'message': response_data.get('detail', response_data.get('title', error_data.get('message', ''))),
                'operation_id': response_data.get('operationID', ''),
                'status': response_data.get('status', http_error.response.status_code),
            })

            if http_error.response.status_code == 422:
                error_data['message'] = self._parse_validation_errors(response_data)

            # Middleware validation format: {"detail": {"errorCode": "RCPT013", "detail": "...", ...}}
            detail = response_data.get('detail') if isinstance(response_data, dict) else None
            if isinstance(detail, dict):
                error_data['code'] = detail.get('errorCode') or detail.get('type') or error_data['code']
                error_data['message'] = detail.get('detail') or detail.get('title') or error_data['message']
                error_data['operation_id'] = detail.get('operationID') or error_data['operation_id']
            elif isinstance(detail, list):
                # Request validation format: {"detail": [{"loc": [...], "msg": "..."}]}
                error_data['message'] = "\n".join(
                    "%s: %s" % (".".join(str(p) for p in item.get('loc', [])), item.get('msg', ''))
                    if isinstance(item, dict) else str(item)
                    for item in detail)

            # Proxy format: {"error": {"code": "SYS002", "message": "[RCPT034] ...", "details": {...}}}
            nested = response_data.get('error') if isinstance(response_data, dict) else None
            if isinstance(nested, dict):
                message = nested.get('message') or error_data['message']
                found = re.search(r'\[([A-Z]{3,5}\d{2,4})\]', message or '')
                error_data['code'] = found.group(1) if found else (nested.get('code') or error_data['code'])
                details = self._to_text(nested.get('details'))
                error_data['message'] = message + ("\n" + details if details else "")

        except json.JSONDecodeError:
            error_data.update({
                'code': 'INVALID_RESPONSE',
                'message': self.env._("The fiscal API returned a response that is not JSON."),
            })

        return error_data

    def _fiscal_fetch_receipts(self, fiscal_day_no, page_size=100, max_pages=50):
        """All receipts the middleware holds for one fiscal day, with ZIMRA's verdict."""
        self.ensure_one()
        receipts = []
        for page in range(max_pages):
            query = urlencode({'fiscal_day_no': fiscal_day_no, 'limit': page_size, 'offset': page * page_size})
            batch = self._api_request('/api/v1/receipts?%s' % query, method='GET')
            if not isinstance(batch, list):
                break
            receipts += batch
            if len(batch) < page_size:
                break
        return receipts

    def _handle_api_error(self, exception, message=None):
        """Handle API request errors and log them appropriately"""
        error_msg = message or self.env._("API request failed")
        
        if isinstance(exception, requests.HTTPError):
            status_code = exception.response.status_code
            try:
                error_details = exception.response.json()
                error_msg += f": HTTP {status_code} - {error_details.get('message', '')}"
            except ValueError:
                error_msg += f": HTTP {status_code} - {exception.response.text}"
        else:
            error_msg += f": {str(exception)}"
        
        _logger.error("%s for device %s: %s", error_msg, self.name, str(exception))
        return error_msg

    def _to_text(self, value):
        """Convert validation payload values into human-readable strings."""
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            # Join list items with commas, ensure each is string
            return ", ".join(str(v) for v in value)
        # Dicts sometimes appear; make them compact
        if isinstance(value, dict):
            # Render as key: value pairs joined by commas
            return ", ".join(f"{k}: {self._to_text(v)}" for k, v in value.items())
        return str(value)

    def _parse_validation_errors(self, response_data):
        """Turn a 422 validation response into readable text."""
        error_map = {
            'DEVICE_NOT_FOUND': self.env._("The device is not registered in FDMS."),
            'INVALID_OPERATION_STATE': self.env._("The device cannot perform this operation in its current state."),
            'MISSING_REQUIRED_FIELD': self.env._("The device is missing required configuration."),
            'AUTH_TOKEN_EXPIRED': self.env._("The access token has expired."),
        }

        error_code = response_data.get('errorCode', '')
        title = error_map.get(error_code, self.env._("FDMS rejected the request."))

        # 'detail' may be string OR list OR dict depending on API
        detail_text = self._to_text(response_data.get('detail', ''))

        message_parts = [title]
        if detail_text:
            message_parts.append(detail_text)

        # Handle 'errors' structure (often a dict of field -> [messages])
        errors = response_data.get('errors', None)
        if isinstance(errors, dict):
            for field, field_errors in errors.items():
                errors_text = self._to_text(field_errors)
                message_parts.append(self.env._("Field %(field)s: %(errors)s", field=field, errors=errors_text))
        elif errors is not None:
            message_parts.append(self._to_text(errors))

        return "\n".join(message_parts)

    def _log_error_details(self, error_data, response_data=None):
        """Store error details in model fields"""
        self.write({
            'last_error_code': error_data['code'],
            'last_error_message': error_data['message'],
            'last_error_status': error_data['status'],
            'last_status_check': fields.Datetime.now(),
        })

    def _format_error_message(self, error_data):
        """Build the error text shown on documents and in alerts."""
        message = self.env._(
            "FDMS error %(code)s (HTTP %(status)s): %(message)s",
            code=error_data['code'], status=error_data['status'], message=error_data['message'])
        if error_data['operation_id']:
            message += "\n" + self.env._("Operation ID: %s", error_data['operation_id'])
        return message

    def _get_auth_headers(self):
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.access_token}'
        }

    # Existing actions with enhanced error handling
    def action_open_day(self):
        self.ensure_one()
        try:
            response = self._api_request('/api/v1/day/open')
            self.write({
                'fiscal_day_no': str(response['fiscalDayNo']),
                'fiscal_day_status': 'FISCALDAYOPENED',
                'last_operation': fields.Datetime.now()
            })
            return self._show_notification(
                self.env._("Fiscal day opened"),
                self.env._("Fiscal day %s is open.", response['fiscalDayNo'])
            )
        except UserError as e:
            return self._show_notification(self.env._("Could not open the fiscal day"), str(e), is_error=True)
    
    def action_close_day(self):
        self.ensure_one()
        try:
            response = self._api_request('/api/v1/day/close')
            
            # Process the response data
            self.write({
                'fiscal_day_status': (response.get('fiscalDayStatus') or '').upper() or False,
                'last_receipt_global_no': response.get('lastReceiptGlobalNo'),
                'fiscal_day_no': response.get('fiscalDayNo'),
                'last_operation': fields.Datetime.now()
            })
            
            # Format success message with response details
            close_time = response.get('fiscalDayClosed', 'N/A')
            message = self.env._(
                "Fiscal day %(day_no)s closed at %(close_time)s. Last receipt: %(receipt_no)s.",
                day_no=response.get('fiscalDayNo', 'N/A'),
                close_time=close_time,
                receipt_no=response.get('lastReceiptNo', 0))
            return self._show_notification(self.env._("Fiscal day closed"), message)
            
        except UserError as e:
            return self._show_notification(self.env._("Could not close the fiscal day"), str(e), is_error=True)

    def action_check_status(self):
        """Refresh the device status from the form button."""
        self.ensure_one()
        try:
            response = self._api_request('/api/v1/status', method='POST')
            self._process_status_response(response)
            return self._show_notification(
                self.env._("Status updated"),
                self.env._("Fiscal day status: %s", self.fiscal_day_status or self.env._("unknown"))
            )
        except UserError as e:
            return self._show_notification(
                self.env._("Status check failed"),
                str(e),
                is_error=True
            )
            
    @api.model
    def cron_check_device_status(self):
        """Check every device's status; one failing device does not stop the rest."""
        devices = self.search([])
        for device in devices:
            try:
                response = device._api_request('/api/v1/status', method='POST')
                device._process_status_response(response)
                _logger.info("Status check succeeded for %s", device.name)
            except Exception as e:
                _logger.error("Status check failed for %s: %s", device.name, str(e))
                device.message_post(
                    body=self.env._("Automatic status check failed: %s", e),
                    subject=self.env._("Status check failed"),
                    partner_ids=device._get_notification_recipients()
                )

    def _cron_refresh_tokens(self):
        """Cron job to refresh tokens for all devices"""
        devices = self.search([])
        for device in devices:
            try:
                device._refresh_token_if_needed()
                _logger.info("Token refreshed successfully for %s", device.name)
            except Exception as e:
                _logger.error("Token refresh failed for %s: %s", device.name, str(e))
                device.message_post(
                    body=self.env._("Automatic token refresh failed: %s", e),
                    subject=self.env._("Token refresh failed"),
                    partner_ids=device._get_notification_recipients()
                )
    # Helper methods
    def _show_notification(self, title, message, is_error=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': 'danger' if is_error else 'success',
                'sticky': is_error
            }
        }

    def _process_status_response(self, response):
        """Store status response data"""
        self.write({
            'fiscal_day_status': (response.get('fiscalDayStatus') or '').upper() or False,
            'last_receipt_global_no': response.get('lastReceiptGlobalNo'),
            'last_receipt_no': response.get('lastReceiptNo'),
            'fiscal_day_counters': response.get('fiscalDayCounters', []),
            'last_status_check': fields.Datetime.now(),
            'fiscal_day_no': response.get('lastFiscalDayNo')
        })
    
    @api.model
    def cron_auto_open_fiscal_day(self):
        """
        Cron job to automatically open fiscal day for all devices
        that have closed fiscal days, running between 00:00-00:30 AM
        """
        current_hour = fields.Datetime.now().hour
        current_minute = fields.Datetime.now().minute
        
        # Only execute between 00:00-00:30 AM
        if current_hour == 0 and current_minute < 30:
            _logger.info("Starting automatic fiscal day opening")
            devices = self.search([('fiscal_day_status', '=ilike', 'FISCALDAYCLOSED')])
            
            for device in devices:
                try:
                    response = device._api_request('/api/v1/day/open')
                    device.write({
                        'fiscal_day_no': str(response['fiscalDayNo']),
                        'fiscal_day_status': 'FISCALDAYOPENED',
                        'last_operation': fields.Datetime.now()
                    })
                    # device.message_post(
                    #     body=self.env._("Fiscal day opened automatically. New fiscal day number: %s") % response['fiscalDayNo'],
                    #     subject=self.env._("Automatic Fiscal Day Open"),
                    #     message_type="notification"
                    # )
                    _logger.info("Successfully opened fiscal day for device %s", device.name)
                    
                except Exception as e:
                    error_message = str(e)
                    _logger.error("Failed to automatically open fiscal day for device %s: %s", device.name, error_message)
                    
                    # device.message_post(
                    #     body=self.env._("Failed to automatically open fiscal day: %s") % error_message,
                    #     subject=self.env._("Automatic Fiscal Day Open Failed"),
                    #     message_type="notification",
                    #     subtype_id=self.env.ref('mail.mt_note').id,
                    #     partner_ids=device.company_id.user_ids.mapped('partner_id').ids
                    # )
    
    @api.model
    def cron_auto_close_fiscal_day(self):
        """
        Cron job to automatically close fiscal day for all devices
        that have open fiscal days, running between 11:30 PM-12:00 AM
        """
        current_hour = fields.Datetime.now().hour
        current_minute = fields.Datetime.now().minute
        
        # Only execute between 11:30 PM and midnight
        if (current_hour == 23 and current_minute >= 30) or (current_hour == 0 and current_minute == 0):
            _logger.info("Starting automatic fiscal day closing")
            devices = self.search([('fiscal_day_status', '=ilike', 'FISCALDAYOPENED')])
            
            for device in devices:
                try:
                    response = device._api_request('/api/v1/day/close')
                    
                    # Process the response data
                    device.write({
                        'fiscal_day_status': (response.get('fiscalDayStatus') or '').upper() or False,
                        'last_receipt_global_no': response.get('lastReceiptGlobalNo'),
                        'fiscal_day_no': response.get('fiscalDayNo'),
                        'last_operation': fields.Datetime.now()
                    })
                    
                    
                    _logger.info("Successfully closed fiscal day for device %s", device.name)
                    
                except Exception as e:
                    error_message = str(e)
                    _logger.error("Failed to automatically close fiscal day for device %s: %s", device.name, error_message)
                    
                    # device.message_post(
                    #     body=self.env._("Failed to automatically close fiscal day: %s") % error_message,
                    #     subject=self.env._("Automatic Fiscal Day Close Failed"),
                    #     message_type="notification",
                    #     subtype_id=self.env.ref('mail.mt_note').id,
                    #     partner_ids=device.company_id.user_ids.mapped('partner_id').ids
                    # )
