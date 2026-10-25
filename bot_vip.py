            columns = {row[1] for row in self.connection.execute('PRAGMA table_info(orders)')}
            if 'notification_attempted' not in columns:
                self.connection.execute('ALTER TABLE orders ADD COLUMN notification_attempted INTEGER NOT NULL DEFAULT 0')

    def close(self):
        self.connection.close()

    def create_order(self, user_id, group_id, amount):
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise PaymentError('ID pengguna Telegram tidak valid.')
        if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id >= 0:
            raise PaymentError('ID grup Telegram harus berupa angka negatif.')
        amount = rupiah(amount)
        order_id = 'vip-' + uuid.uuid4().hex
        with self.lock, self.connection:
            self.connection.execute('INSERT INTO orders (order_id, user_id, group_id, amount) VALUES (?, ?, ?, ?)', (order_id, user_id, group_id, amount))
        return self.order(order_id)

    def order(self, order_id):
        with self.lock:
            row = self.connection.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,)).fetchone()
            return dict(row) if row else None

    def has_access(self, user_id, group_id):
        with self.lock:
            return self.connection.execute('SELECT 1 FROM access WHERE user_id = ? AND group_id = ?', (user_id, group_id)).fetchone() is not None

    def access_orders(self, user_id):
        with self.lock:
            return [dict(row) for row in self.connection.execute('SELECT orders.* FROM access JOIN orders USING (order_id) WHERE access.user_id = ?', (user_id,))]

    def claim_notification(self, order_id):
        with self.lock, self.connection:
            return self.connection.execute("UPDATE orders SET notification_attempted = 1 WHERE order_id = ? AND state = 'paid' AND notification_attempted = 0", (order_id,)).rowcount == 1

    def add_invite(self, order_id, link, expires_at):
        with self.lock, self.connection:
            order = self.order(order_id)
            if order is None or order['state'] != 'paid':
                raise PaymentError('Pesanan belum dibayar.')
            self.connection.execute('INSERT INTO invites (link, order_id, expires_at) VALUES (?, ?, ?)', (link, order_id, expires_at))

    def invite_order(self, link):
        with self.lock:
            row = self.connection.execute('SELECT orders.*, invites.expires_at FROM invites JOIN orders USING (order_id) WHERE invites.link = ?', (link,)).fetchone()
            return dict(row) if row else None

    def _verified_status(self, order_id, state, charge_id):
        with self.lock, self.connection:
            order = self.order(order_id)
            if order is None:
                raise PaymentError('Pesanan tidak ditemukan.')
            if order['state'] == 'paid':
                if state == 'paid' and order['charge_id'] != charge_id:
                    raise PaymentError('ID transaksi tidak cocok dengan pembayaran tersimpan.')
                return self.order(order_id)
            if state == 'paid':
                try:
                    self.connection.execute("UPDATE orders SET state = 'paid', charge_id = ? WHERE order_id = ?", (charge_id, order_id))
                except sqlite3.IntegrityError:
                    raise PaymentError('ID transaksi sudah digunakan pesanan lain.') from None
                self.connection.execute('INSERT OR IGNORE INTO access (user_id, group_id, order_id) VALUES (?, ?, ?)', (order['user_id'], order['group_id'], order_id))
            else:
                self.connection.execute('UPDATE orders SET state = ? WHERE order_id = ?', (state, order_id))
            return self.order(order_id)

class Payments:

    def __init__(self, store, server_key, *, production=False, merchant_id=None, transport=None):
        if not server_key:
            raise PaymentError('MIDTRANS_SERVER_KEY belum diatur.')
        self.store = store
        self.server_key = server_key
        self.merchant_id = merchant_id
        self.base_url = 'https://api.midtrans.com' if production else 'https://api.sandbox.midtrans.com'
        self.client = httpx.Client(auth=(server_key, ''), timeout=30, transport=transport, headers={'Accept': 'application/json'})

    def close(self):
        self.client.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, self.base_url + path, **kwargs)
            if response.status_code not in (200, 201):
                raise PaymentError(f'Midtrans memberikan HTTP {response.status_code}.')
            data = response.json()
            if not isinstance(data, dict) or str(data.get('status_code')) not in ('200', '201'):
                raise PaymentError('Permintaan belum diterima Midtrans.')
            return data
        except (httpx.HTTPError, ValueError) as error:
            raise PaymentError(f'Permintaan Midtrans gagal ({type(error).__name__}).') from None

    def create_qris(self, order_id):
        order = self.store.order(order_id)
        if order is None or order['state'] != 'pending':
            raise PaymentError('Pesanan tidak tersedia untuk pembayaran baru.')
        data = self._request('POST', '/v2/charge', json={'payment_type': 'qris', 'transaction_details': {'order_id': order_id, 'gross_amount': order['amount']}, 'qris': {'acquirer': 'gopay'}})
        self._validate_order(data, order)
        qr_url = next((action.get('url') for action in data.get('actions', []) if action.get('name') == 'generate-qr-code'), None)
        parsed = urlsplit(qr_url or '')
        if parsed.scheme != 'https' or parsed.hostname != urlsplit(self.base_url).hostname or parsed.username or parsed.password or (parsed.port not in (None, 443)):
            raise PaymentError('Alamat QRIS tidak sesuai dengan server Midtrans.')
        return {'order_id': order_id, 'amount': order['amount'], 'qr_url': qr_url}

    def _validate_order(self, data, order):
        if data.get('order_id') != order['order_id'] or rupiah(data.get('gross_amount')) != order['amount'] or data.get('currency') != 'IDR' or (data.get('payment_type') != 'qris'):
            raise PaymentError('Detail transaksi tidak cocok dengan pesanan.')
        if self.merchant_id and data.get('merchant_id') != self.merchant_id:
            raise PaymentError('Merchant pembayaran tidak cocok.')

    def qr_image(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != urlsplit(self.base_url).hostname or parsed.username or parsed.password or (parsed.port not in (None, 443)):
            raise PaymentError('Alamat gambar QRIS tidak valid.')
        try:
            with self.client.stream('GET', url) as response:
                if response.status_code != 200 or not response.headers.get('content-type', '').startswith('image/'):
                    raise PaymentError('Gambar QRIS belum tersedia.')
                result = bytearray()
                for chunk in response.iter_bytes():
                    result.extend(chunk)
                    if len(result) > 2 * 1024 * 1024:
                        raise PaymentError('Gambar QRIS terlalu besar.')
                if not result:
                    raise PaymentError('Gambar QRIS kosong.')
                return bytes(result)
        except httpx.HTTPError as error:
            raise PaymentError(f'Pengambilan gambar QRIS gagal ({type(error).__name__}).') from None

    def check(self, order_id, *, user_id=None):
        order = self.store.order(order_id)
        if order is None or (user_id is not None and order['user_id'] != user_id):
            raise PaymentError('Pesanan tidak ditemukan untuk pengguna ini.')
        data = self._request('GET', f'/v2/{order_id}/status')
        self._validate_order(data, order)
        state = data.get('transaction_status')
        if state == 'settlement' and data.get('fraud_status', 'accept') == 'accept':
            charge_id = data.get('transaction_id')
            if not isinstance(charge_id, str) or not charge_id:
                raise PaymentError('ID transaksi pembayaran tidak tersedia.')
            return self.store._verified_status(order_id, 'paid', charge_id)
        if state in ('cancel', 'deny', 'expire', 'failure'):
            return self.store._verified_status(order_id, 'failed', None)
        return self.store._verified_status(order_id, 'pending', None)

    def notification(self, data):
        if not isinstance(data, dict):
            raise PaymentError('Notifikasi pembayaran tidak valid.')
        fields = ('order_id', 'status_code', 'gross_amount', 'signature_key')
        if any((not isinstance(data.get(field), str) or not data[field] for field in fields)):
            raise PaymentError('Notifikasi pembayaran tidak lengkap.')
        expected = hashlib.sha512((data['order_id'] + data['status_code'] + data['gross_amount'] + self.server_key).encode()).hexdigest()
        if not hmac.compare_digest(expected, data['signature_key']):
            raise PaymentError('Tanda tangan notifikasi pembayaran tidak valid.')
        return self.check(data['order_id'])
from dataclasses import dataclass

@dataclass(frozen=True)
class Plan:
    name: str
    amount: int
PLANS = {'indo_premium': Plan('INDO PREMIUM', 30000), 'gay_lesby_acak': Plan('GAY/LESBY ACAK', 50000), 'chil_vip': Plan('CHIL VIP', 75000), 'full_paket': Plan('FULL PAKET', 95000)}

def create_plan_order(store, user_id, plan_id, group_ids):
    """Use a server-owned price and group mapping, never a buyer's amount."""
    plan = PLANS.get(plan_id)
    if plan is None:
        raise PaymentError('Paket VIP tidak ditemukan.')
    if plan_id not in group_ids:
        raise PaymentError('Grup Telegram untuk paket ini belum dikonfigurasi.')
    return store.create_order(user_id, group_ids[plan_id], plan.amount)
import json
import logging
from http.server import BaseHTTPRequestHandler
logger = logging.getLogger(__name__)

def notification_handler(payments, environment, on_paid=None):

    class Handler(BaseHTTPRequestHandler):

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *args):
            pass

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
