import random
from gmssl import sm3
import time
import functools
from copy import deepcopy

chunk = []
size = 0
reg = [1937774191, 1226093241, 388252375, 3666478592, 2842636476, 372324522, 3817729613, 2969243214]


def reset():
    global chunk, size, reg
    chunk = []
    size = 0
    reg = [1937774191, 1226093241, 388252375, 3666478592, 2842636476, 372324522, 3817729613, 2969243214]


def fromCharCode(n):
    return chr(int(n) & 0xFFFFFFFF)


def be(e, r):
    r %= 32
    return (e << r | e >> (32 - r)) & 0xFFFFFFFF


def de(e):
    if 0 <= e < 16:
        return 2043430169
    elif 16 <= e < 64:
        return 2055708042
    else:
        return 0


def pe(e, r, t, n):
    if 0 <= e < 16:
        return (r ^ t ^ n) & 0xFFFFFFFF
    elif 16 <= e < 64:
        return ((r & t) | (r & n) | (t & n)) & 0xFFFFFFFF
    else:
        return 0


def he(e, r, t, n):
    if 0 <= e < 16:
        return (r ^ t ^ n) & 0xFFFFFFFF
    elif 16 <= e < 64:
        return ((r & t) | (~r & n)) & 0xFFFFFFFF
    else:
        return 0


def get_fill():
    global size, chunk
    a = 8 * size
    chunk.append(128)
    f = len(chunk) % 64
    if 64 - f < 8:
        f -= 64
    while f < 56:
        chunk.append(0)
        f += 1
    for i in range(4):
        o = a // 4294967296
        chunk.append((o >> 8 * (3 - i)) & 255)
    for i in range(4):
        chunk.append((a >> 8 * (3 - i)) & 255)


def get_ua_encrypt(a, c):
    e, b, d, f = [], [], 0, ""
    b = [i for i in range(256)]
    for i in range(256):
        d = (d + b[i] + ord(a[i % len(a)])) % 256
        e = b[i]
        b[i] = b[d]
        b[d] = e
    t = 0
    d = 0
    for i in range(len(c)):
        t = (t + 1) % 256
        d = (d + b[t]) % 256
        e = b[t]
        b[t] = b[d]
        b[d] = e
        f += fromCharCode(ord(c[i]) ^ b[(b[t] + b[d]) % 256])
    return f


def compress_process_data(t):
    r = [0] * 132
    for i in range(16):
        r[i] = t[4 * i] << 24
        r[i] |= t[4 * i + 1] << 16
        r[i] |= t[4 * i + 2] << 8
        r[i] |= t[4 * i + 3]
        r[i] = r[i] & 0xFFFFFFFF
    for i in range(16, 68):
        a = r[i - 16] ^ r[i - 9] ^ be(r[i - 3], 15)
        a = a ^ be(a, 15) ^ be(a, 23)
        r[i] = (a ^ be(r[i - 13], 7) ^ r[i - 6]) & 0xFFFFFFFF
    for i in range(64):
        r[i + 68] = (r[i] ^ r[i + 4]) & 0xFFFFFFFF
    return r


def compress(t):
    global reg
    f = compress_process_data(t)
    i = deepcopy(reg)
    for o in range(64):
        c = be(i[0], 12) + i[4] + be(de(o), o)
        c = (4294967295 & c) & 0xFFFFFFFF
        c = be(c, 7)
        s = (c ^ be(i[0], 12)) & 0xFFFFFFFF
        u = pe(o, i[0], i[1], i[2])
        u = u + i[3] + s + f[o + 68]
        u = (4294967295 & u) & 0xFFFFFFFF
        l = he(o, i[4], i[5], i[6])
        l = l + i[7] + c + f[o]
        l = (4294967295 & l) & 0xFFFFFFFF
        i[3], i[2], i[1], i[0] = i[2], be(i[1], 9), i[0], u
        i[7], i[6], i[5], i[4] = i[6], be(i[5], 19), i[4], (l ^ be(l, 9) ^ be(l, 17)) & 0xFFFFFFFF
    for b in range(8):
        reg[b] = (reg[b] ^ i[b]) & 0xFFFFFFFF


def write(e):
    global size, chunk
    a = [ord(c) for c in e] if isinstance(e, str) else e
    size += len(a)
    f = 64 - len(chunk)
    if len(a) < f:
        chunk += a
    else:
        chunk += a[:f]
        while len(chunk) >= 64:
            compress(chunk)
            if f < len(a):
                chunk = a[f:min(f + 64, len(a))]
            else:
                chunk = []
            f += 64


def get_32_num(params):
    global chunk, size, reg
    write(params)
    get_fill()
    for i in range(0, len(chunk), 64):
        compress(chunk[i:i + 64])
    o = [0] * 32
    for i in range(8):
        c = reg[i]
        o[4 * i + 3] = (255 & c)
        c >>= 8
        o[4 * i + 2] = (255 & c)
        c >>= 8
        o[4 * i + 1] = (255 & c)
        c >>= 8
        o[4 * i] = (255 & c)
    reset()
    return o


def get_first_salt_str():
    time_stemp = int(random.random() * 10000)
    a = time_stemp & 255
    b = (time_stemp >> 8) & 255
    c = (a & 170) | (3 & 85)
    d = (a & 85) | (3 & 170)
    e = (b & 170) | (45 & 85)
    f = (b & 85) | (45 & 170)
    return from_char_codes([c, d, e, f])


def get_secode_salt_str():
    time_stemp = int(random.random() * 10000)
    a = time_stemp & 255
    b = (time_stemp >> 8) & 255
    c = (a & 170) | (1 & 85)
    d = (a & 85) | (1 & 170)
    e = (b & 170) | (0 & 85)
    f = (b & 85) | (0 & 170)
    return from_char_codes([c, d, e, f])


def get_three_salt_str():
    time_stemp = int(random.random() * 10000)
    a = time_stemp & 255
    b = (time_stemp >> 8) & 255
    c = (a & 170) | (1 & 85)
    d = (a & 85) | (1 & 170)
    e = (b & 170) | (1 & 85)
    f = (b & 85) | (1 & 170)
    return from_char_codes([c, d, e, f])


def reduce_list(array):
    result = functools.reduce(lambda x, y: int(x) ^ int(y), array)
    return result


def from_char_codes(array):
    return ''.join(chr(code) for code in array)


def gmssl_sm3_encode(params: [str, list]):
    if isinstance(params, list):
        msg_list = bytearray(params)
    else:
        msg_list = [i for i in bytes(params.encode())]
    hash_object = sm3.sm3_hash(msg_list)
    hash_bytes = bytes.fromhex(hash_object)
    hash_list = [byte for byte in hash_bytes]
    return hash_list


def get_ab(params, ua, data=''):
    '''
    如果是get，请求，data就为空，post的参数也要进行计算，所以这里可以这样兼容
    这里的三个时间一定要注意，不是拿的实时的时间来进行计算
    :param params: 提交的参数字符串
    :param ua: 浏览器头
    :param data: 提交的data字符串
    :return:
    '''
    slat = 'cus'
    envCode = [0, 1, 12]  # 每个网站的env_code不一样
    aid = 6383  # aid也不一样
    pageId = 11881  # pageid也不一样
    timestamp1 = int(time.time() * 1000)
    timestamp2 = timestamp1 - random.randint(50, 100)
    params_str = params + slat
    data_str = data + slat
    params_list = gmssl_sm3_encode(gmssl_sm3_encode(params_str))
    if envCode[-1] == 12:
        encode_env_str = "\u0000\u0001\f"
    elif envCode[-1] == 14:
        encode_env_str = "\u0000\u0001\u000e"
    else:
        encode_env_str = "\u0000\u0001\u0000"
    ua_str = get_ua_tostring(get_ua_encrypt(encode_env_str, ua))
    ua_list = gmssl_sm3_encode(ua_str)
    data_list = gmssl_sm3_encode(gmssl_sm3_encode(data_str))
    envCode_first_list = [envCode[0] >> 24 & 255, envCode[0] >> 16 & 255, envCode[0] >> 8 & 255, envCode[0] & 255]
    envCode_second_list = [envCode[1] >> 8 & 255, envCode[1] & 255, envCode[1] >> 24 & 255, envCode[1] >> 16 & 255, ]
    envCode_third_list = [envCode[2] >> 24 & 255, envCode[2] >> 16 & 255, envCode[2] >> 8 & 255, envCode[2] & 255]
    pageId_list = [pageId >> 24 & 255, pageId >> 16 & 255, pageId >> 8 & 255, pageId & 255]
    aid_list = [aid >> 8 & 255, aid & 255, aid >> 24 & 255, aid >> 16 & 255]
    timestamp1_list = [timestamp1 >> 24 & 255, timestamp1 >> 16 & 255, timestamp1 >> 8 & 255, timestamp1 & 255]
    timestamp1_int_list = [timestamp1 >> 32, timestamp1 >> 40]
    timestamp2_list = [timestamp2 >> 24 & 255, timestamp2 >> 16 & 255, timestamp2 >> 8 & 255, timestamp2 & 255]
    timestamp2_int_list = [timestamp2 >> 32, timestamp2 >> 40]
    env_array = [49, 57, 50, 48, 124, 57, 53, 51, 124,
                 49, 57, 50, 48, 124, 49, 48, 52, 48, 124, 48, 124, 48, 124, 48, 124, 48, 124,
                 49, 57, 50, 48, 124, 49, 48, 52, 48, 124,
                 49, 57, 50, 48, 124, 49, 48, 56, 48, 124,
                 49, 57, 50, 48, 124, 57, 53, 51, 124, 50, 52, 124, 50, 52, 124, 87, 105, 110, 51, 50]
    salt_array = [
        44,
        timestamp2_list[0], pageId_list[0], envCode_first_list[0], envCode_second_list[0], envCode_third_list[0],
        aid_list[0], params_list[21], data_list[21], pageId_list[1], ua_list[23],
        timestamp2_list[1], envCode_first_list[1], pageId_list[2], pageId_list[3], envCode_second_list[1],
        envCode_third_list[1], aid_list[1], params_list[22], data_list[22], ua_list[24],
        timestamp2_list[2], envCode_first_list[2], envCode_second_list[2], aid_list[2], envCode_third_list[2],
        timestamp2_list[3], envCode_first_list[3], envCode_second_list[3], envCode_third_list[3],
        timestamp1_list[0], timestamp1_list[1], aid_list[3], timestamp1_list[2], timestamp1_list[3],
        3,
        timestamp1_int_list[0], timestamp1_int_list[1], timestamp2_int_list[0], timestamp2_int_list[1],
        len(env_array), 0, 0, 0
    ]
    result_array = salt_array + env_array
    last_num = reduce_list(salt_array)
    result_array.append(last_num)
    first_result_str = get_first_salt_str()
    second_result_str = get_secode_salt_str()
    three_result_str = get_three_salt_str()
    four_result_str = from_char_codes(result_array)
    result = get_ua_encrypt("y", four_result_str)
    encrypt_str = first_result_str + second_result_str + three_result_str + result
    return get_encrypt(encrypt_str)


def get_ua_tostring(garbledString):
    short_str = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
    ABogus = ""
    index_list1 = [64 ** i * 63 for i in range(3, -1, -1)]
    garbledString1 = garbledString[:len(garbledString) // 3 * 3]
    garbledString2 = garbledString[len(garbledString) // 3 * 3:]
    for i in range(0, len(garbledString) // 3 * 3, 3):
        charCodeAtNum0 = ord(garbledString1[i])
        charCodeAtNum1 = ord(garbledString1[i + 1])
        charCodeAtNum2 = ord(garbledString1[i + 2])
        baseNum = charCodeAtNum2 | charCodeAtNum1 << 8 | charCodeAtNum0 << 16
        for j in zip(index_list1, list(range(18, -1, -6))):
            ABogus += short_str[baseNum & j[0]] if j[1] == 0 else short_str[(baseNum & j[0]) >> j[1]]
    if garbledString2:
        if len(garbledString2) == 1:
            charCodeAtNum3 = ord(garbledString2) << 16
            ABogus += short_str[(charCodeAtNum3 & 16515072) >> 18]
            ABogus += short_str[(charCodeAtNum3 & 258048) >> 12]
        else:
            for i in range(0, len(garbledString2), 2):
                charCodeAt1 = ord(garbledString2[i])
                charCodeAt2 = ord(garbledString2[i + 1])
                baseNum = charCodeAt2 << 8 | charCodeAt1 << 16
                for j in zip(index_list1, list(range(18, 0, -6))):
                    ABogus += short_str[baseNum & j[0]] if j[1] == 0 else short_str[(baseNum & j[0]) >> j[1]]
        ABogus = ABogus.ljust((len(garbledString) // 3 + 1) * 4, '=')
    return ABogus


def get_encrypt(garbledString, source='s3'):
    s3 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
    s4 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
    short_str = s3 if source == 's3' else s4
    ABogus = ""
    index_list1 = [64 ** i * 63 for i in range(3, -1, -1)]
    garbledString1 = garbledString[:len(garbledString) // 3 * 3]
    garbledString2 = garbledString[len(garbledString) // 3 * 3:]
    for i in range(0, len(garbledString) // 3 * 3, 3):
        charCodeAtNum0 = ord(garbledString1[i])
        charCodeAtNum1 = ord(garbledString1[i + 1])
        charCodeAtNum2 = ord(garbledString1[i + 2])
        baseNum = charCodeAtNum2 | charCodeAtNum1 << 8 | charCodeAtNum0 << 16
        for j in zip(index_list1, list(range(18, -1, -6))):
            ABogus += short_str[baseNum & j[0]] if j[1] == 0 else short_str[(baseNum & j[0]) >> j[1]]
    if len(garbledString2) == 1:
        charCodeAtNum3 = ord(garbledString2) << 16
        ABogus += short_str[(charCodeAtNum3 & 16515072) >> 18]
        ABogus += short_str[(charCodeAtNum3 & 258048) >> 12]
    else:
        for i in range(0, len(garbledString2), 2):
            charCodeAt1 = ord(garbledString2[i])
            charCodeAt2 = ord(garbledString2[i + 1])
            baseNum = charCodeAt2 << 8 | charCodeAt1 << 16
            for j in zip(index_list1, list(range(18, 0, -6))):
                ABogus += short_str[baseNum & j[0]] if j[1] == 0 else short_str[(baseNum & j[0]) >> j[1]]
    ABogus = ABogus.ljust((len(garbledString) // 3 + 1) * 4, '=')
    return ABogus
