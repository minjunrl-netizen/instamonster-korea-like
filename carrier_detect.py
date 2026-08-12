"""
통신사 자동 감지

각 슬롯의 외부 IP를 ASN(통신사 고유번호)으로 조회해서
SKT / KT / LGU+ 중 어느 통신사인지 자동 판별한다.

유심이 어느 통신사인지 몰라도 IP만 보면 알 수 있으므로,
슬롯 이름을 손으로 지정할 필요가 없다.

한국 통신사 ASN:
  SKT  - AS9644 (SK Telecom), 모바일
  KT   - AS4766 (Korea Telecom / KIXS-AS-KR)
  LGU+ - AS3786 (LG DACOM), AS17858 (LG POWERCOMM), AS10265 등
"""

import logging

import requests

logger = logging.getLogger(__name__)

# ISP/ASName 문자열 → 통신사 매핑 (소문자 부분일치)
CARRIER_KEYS = {
    "SKT": ["sk telecom", "sktelecom", "skt", "sk broadband", "skbroadband", "as9644", "as9318"],
    "KT": ["korea telecom", "kixs", "kt corporation", "as4766", " kt ", "olleh"],
    "LGU": ["lg uplus", "lguplus", "lg u+", "lg dacom", "dacom", "powercomm",
            "lg powercomm", "as3786", "as17858", "as10265", "uplus"],
}


def carrier_from_text(*texts: str) -> str:
    """ISP/AS 문자열들에서 통신사를 판별한다. 못 찾으면 '기타'."""
    blob = " ".join(t.lower() for t in texts if t)
    # LGU를 먼저 (KT의 부분문자열 오판 방지: 'lg'가 'telecom'과 안 겹치게)
    for carrier in ("SKT", "LGU", "KT"):
        for key in CARRIER_KEYS[carrier]:
            if key in blob:
                return "LGU+" if carrier == "LGU" else carrier
    return "기타"


def lookup_ip(ip: str) -> dict:
    """
    IP의 통신사/ASN 정보를 조회한다.
    ip-api.com 우선, 실패 시 ipinfo.io 폴백.
    """
    if not ip or ip in ("error", "unknown"):
        return {"carrier": "?", "isp": "", "asn": "", "mobile": None}

    # 1차: ip-api.com (무료, mobile 플래그 제공)
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}"
            "?fields=status,isp,org,as,asname,mobile,country",
            timeout=10,
        ).json()
        if r.get("status") == "success":
            carrier = carrier_from_text(r.get("isp", ""), r.get("as", ""),
                                        r.get("asname", ""), r.get("org", ""))
            return {
                "carrier": carrier,
                "isp": r.get("isp", ""),
                "asn": r.get("as", ""),
                "mobile": r.get("mobile"),
                "country": r.get("country", ""),
            }
    except Exception as e:
        logger.debug(f"ip-api 조회 실패({ip}): {e}")

    # 2차: ipinfo.io
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=10).json()
        org = r.get("org", "")
        return {
            "carrier": carrier_from_text(org),
            "isp": org,
            "asn": org,
            "mobile": None,
            "country": r.get("country", ""),
        }
    except Exception as e:
        logger.debug(f"ipinfo 조회 실패({ip}): {e}")

    return {"carrier": "?", "isp": "", "asn": "", "mobile": None}


def detect_slot_carriers(provider) -> list[dict]:
    """
    프로바이더의 모든 슬롯에 대해 IP → 통신사를 감지한다.

    반환: [{slot_index, port, current_name, ip, carrier, asn, mobile}, ...]
    """
    results = []
    for i, slot in enumerate(provider.slots):
        ip = provider.get_current_ip(i)
        info = lookup_ip(ip)
        results.append({
            "slot_index": i,
            "port": slot.get("port"),
            "current_name": slot.get("name", f"slot-{i}"),
            "modem": slot.get("modem", i + 1),
            "ip": ip,
            "carrier": info["carrier"],
            "asn": info["asn"],
            "isp": info["isp"],
            "mobile": info["mobile"],
        })
    return results


def suggest_slot_name(index: int, carrier: str) -> str:
    """감지된 통신사로 슬롯 이름을 만든다: 유심01-SKT"""
    tag = carrier if carrier in ("SKT", "KT", "LGU+") else "기타"
    return f"유심{index + 1:02d}-{tag}"
