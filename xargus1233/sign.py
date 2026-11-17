import time
from hashlib import md5

from xargus1233.argus import Argus
from xargus1233.gorgon import Gorgon
from xargus1233.ladon import Ladon


def sign1233(params, data: str = None, sec_device_id: str = "", cookie: str or None = None, aid: int = 1233, license_id: int = 1611921764, sdk_version_str: str = "v04.04.05-ov-android", sdk_version: int = 134744640, platform: int = 0, unix: int = None):
    x_ss_stub = md5(data.encode('utf-8')).hexdigest() if data != None else None
    if not unix: unix = int(time.time())
    return Gorgon(params.split('?')[1], unix, data, cookie).get_value() | {
        "X-Ladon": Ladon.encrypt(unix, license_id, aid),
        "X-Argus": Argus.get_sign(params.split('?')[1], x_ss_stub, unix,
                                  platform=platform,
                                  aid=aid,
                                  license_id=license_id,
                                  sec_device_id=sec_device_id,
                                  sdk_version=sdk_version_str,
                                  sdk_version_int=sdk_version
                                  )
    }
