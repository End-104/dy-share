import binascii
import random
import time
import uuid
from hashlib import md5
from urllib.parse import parse_qs

from pysmx.SM3 import SM3

from xargus1128.argus import Argus
from xargus1128.gorgon import Gorgon
from xargus1128.ladon import Ladon


def calc_sm3(data):
    sm3 = SM3()
    sm3.update(data)
    return sm3.digest()


def x_tt_trace_id(device_id):
    hex_value = format(int(device_id), 'x')
    length = format(len(hex_value), '02x')
    rand = uuid.uuid4().hex[:17 - len(hex_value) + 1]
    current_time = format(int(time.time() * 1000) % 4294967295, '08x')
    front = current_time + length + hex_value + rand + "0468"
    return "00-" + front + "-" + front[:16] + "-01"


def sign(params: str
         , app_version: str = None
         , payload: str | bytes = None
         , cookie: str or None = None
         , aid: int = 1128
         , license_id: str = '1588093228'
         , sdk_version_str: str = "v04.03.03-ml-android"
         , sdk_version: int = 134612480
         , sec_device_id: str = ""
         , call_type: int = 624
         , sign_count: int = random.randint(100, 500)
         , report_count: int = 2
         , setting_count: int = random.randint(10000, 20000)
         ):
    """
    :param call_type: 还蛮重要的，会验证！
    :param app_version: App版本
    :param params: url后面的query
    :param payload: post请求的body接受str和bytes两种类型
    :param sec_device_id: 加密设备id，这兄弟无所谓
    :param cookie:
    :param aid:
    :param license_id: 不确定有啥用大家好像都一样
    :param sdk_version_str:
    :param sdk_version:
    :param report_count: 有些校验比较严的需要这三兄弟
    :param setting_count: 有些校验比较严的需要这三兄弟
    :param sign_count: 有些校验比较严的需要这三兄弟
    :return:
    """

    def calc_sm3(data):
        sm3 = SM3()
        sm3.update(data)
        return sm3.digest()

    if type(payload) is bytes:
        x_ss_stub = md5(payload).hexdigest() if payload is not None else None
    else:
        x_ss_stub = md5(payload.encode('utf-8')).hexdigest() if payload is not None else None

    unix = int(time.time())
    params_dict = parse_qs(params)
    params_dict = {key: value[0] for key, value in params_dict.items()}
    device_id = params_dict.get('device_id', '')
    app_version = params_dict.get('version_name') if not app_version else app_version

    x_gorgon = Gorgon(params, unix, payload, cookie).get_value()
    khronos = x_gorgon['X-Khronos']
    query_hash = calc_sm3(params)[:6]

    if x_ss_stub:
        body_hash = calc_sm3(binascii.unhexlify(x_ss_stub))[:6]
        x_gorgon.update({'X-SS-STUB': x_ss_stub.upper()})
    else:
        body_hash = calc_sm3(b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")[:6]

    x_argus = Argus()
    x_argus.gen_xa_pb(int(khronos), query_hash, body_hash, device_id,
                      license_id, app_version, sdk_version_str, sdk_version, sec_device_id, call_type, sign_count,
                      report_count, setting_count)
    x_gorgon.update({
        "X-Ladon": Ladon.encrypt(x_gorgon['X-Khronos'], int(license_id), aid),
        "X-Argus": x_argus.encrypt(),
        'x-tt-trace-id': x_tt_trace_id(device_id)
    })
    return x_gorgon


def sign_1615(params, payload, cookie):
    return sign(
        params.split('?')[1],
        app_version='16.4.15',
        payload=payload,
        cookie=cookie,
        aid=1128,
        license_id='1588093228',
        sdk_version_str='v04.03.03-ml-android',
        sdk_version=134612480,
        sec_device_id='',
        call_type=624,
        sign_count=random.randint(100, 9999),
        report_count=random.randint(1388734, 9999999),
        setting_count=random.randint(1388734, 9999999)
    )


def sign_2290(params, payload, cookie):
    return sign(
        params.split('?')[1],
        app_version='22.9.0',
        payload=payload,
        cookie=cookie,
        aid=1128,
        license_id='1588093228',
        sdk_version_str='v04.03.03-ml-android',
        sdk_version=134612480,
        sec_device_id='',
        call_type=624,
        sign_count=random.randint(100, 9999),
        report_count=random.randint(1388734, 9999999),
        setting_count=random.randint(1388734, 9999999)
    )


def sign_3050(params, payload, cookie):
    return sign(
        params.split('?')[1],
        app_version='30.5.0',
        payload=payload,
        cookie=cookie,
        aid=1128,
        license_id='1588093228',
        sdk_version_str='v04.03.03-ml-android',
        sdk_version=134612480,
        sec_device_id='',
        call_type=624,
        sign_count=random.randint(100, 9999),
        report_count=random.randint(1388734, 9999999),
        setting_count=random.randint(1388734, 9999999)
    )


def sign_3202(params, payload, cookie):
    return sign(
        params.split('?')[1],
        app_version='32.2.2',
        payload=payload,
        cookie=cookie,
        aid=664226,
        license_id='1378330313',
        sdk_version_str='v04.07.00-ml-android',
        sdk_version=67567616,
        sec_device_id='',
        call_type=624,
        sign_count=random.randint(100, 9999),
        report_count=random.randint(1388734, 9999999),
        setting_count=random.randint(1388734, 9999999)
    )


def sign_9901(params, payload, cookie):
    return sign(
        params.split('?')[1],
        app_version='9.9.1504',
        payload=payload,
        cookie=cookie,
        aid=2955,
        license_id='1378330313',
        sdk_version_str='v04.07.00-ml-android',
        sdk_version=67567616,
        sec_device_id='',
        call_type=624,
        sign_count=random.randint(100, 9999),
        report_count=random.randint(1388734, 9999999),
        setting_count=random.randint(1388734, 9999999)
    )
