import ipaddress

from ninja.conf import settings as ninja_settings

# провайдер выдает абоненту IPv6 целой сетью /64, поэтому лимиты считают ее одним клиентом
IPV6_CLIENT_PREFIX = 64


def _forwarded_address(request) -> str | None:
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    remote_addr = request.META.get('REMOTE_ADDR')
    num_proxies = ninja_settings.NUM_PROXIES

    if num_proxies is None:
        return "".join(xff.split()) if xff else remote_addr
    if num_proxies == 0 or xff is None:
        return remote_addr

    addrs = xff.split(',')
    return addrs[-min(num_proxies, len(addrs))].strip()


def _client_identity(address: str | None) -> str | None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return address
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    host_bits = ip.max_prefixlen - IPV6_CLIENT_PREFIX
    return str(ipaddress.IPv6Address(int(ip) >> host_bits << host_bits))


def get_client_ip(request) -> str | None:
    return _client_identity(_forwarded_address(request))
