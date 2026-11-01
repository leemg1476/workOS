"""Synthetic weekly inputs and independent fixture labels for the human-effort PoC.

Labels are fixture ground truth, not human-validated production labels.
"""
PERIODS={'week1':'voc-poc-week1-v1','week2':'voc-poc-week2-v1'}
CATEGORIES=('billing','delivery','support','unclassified')

_WEEK1=[
 ('billing','지난달 결제 내역이 주문 금액과 달라 확인해 주세요.'),
 ('delivery','택배 조회 화면이 사흘째 같은 상태입니다.'),
 ('support','상담 연결을 요청했는데 답을 받지 못했습니다.'),
 ('billing','취소한 주문의 환불 예정일을 알고 싶습니다.'),
 ('delivery','상품 배송 주소를 변경할 수 있나요?'),
 ('support','문의 접수 후 담당자의 회신이 늦습니다.'),
 ('billing','청구 금액이 두 번 표시됩니다.'),
 ('delivery','주문한 물건의 도착 예정일이 지났습니다.'),
 ('support','상담 가능 시간을 알려 주세요.'),
 ('billing','결제 수단을 다른 카드로 바꾸고 싶습니다.'),
 ('delivery','택배가 다른 장소에 놓여 있었습니다.'),
 ('support','응대 내용이 앞서 안내받은 내용과 다릅니다.'),
 ('billing','부분 환불 금액을 확인할 수 있나요?'),
 ('delivery','배송 상태 알림을 받지 못했습니다.'),
 ('support','문의 답변에서 요청한 항목이 빠졌습니다.'),
 ('billing','청구서에 할인 금액이 빠진 것 같습니다.'),
 ('delivery','도착 안내 없이 물건이 반송됐습니다.'),
 ('support','상담 대기 시간이 너무 길었습니다.'),
 ('billing','환불이 접수됐는지 확인해 주세요.'),
 ('delivery','택배 수령 날짜를 바꾸고 싶습니다.'),
 ('support','응대 중 설명이 충분하지 않았습니다.'),
 ('billing','결제 승인 취소 내역이 보이지 않습니다.'),
 ('delivery','배송 과정에서 상자가 손상됐습니다.'),
 ('support','문의 답변을 이메일로 다시 받을 수 있나요?'),
]
_WEEK2=[
 ('delivery','오늘 도착한다고 안내받은 물건이 아직 오지 않았습니다.'),
 ('support','상담 예약을 잡았는데 연락이 오지 않았어요.'),
 ('billing','환불 처리 결과와 입금 날짜를 알려 주세요.'),
 ('delivery','택배 기사에게 전달할 수령 요청이 있습니다.'),
 ('support','응대 담당자가 제 요청을 잘못 이해했습니다.'),
 ('billing','청구된 금액에 사용하지 않은 항목이 있습니다.'),
 ('delivery','배송지가 이전 주소로 표시되어 수정이 필요합니다.'),
 ('support','문의 접수 번호는 받았지만 답변이 없습니다.'),
 ('billing','결제 승인액이 실제 주문액보다 큽니다.'),
 ('delivery','예상 도착 날짜가 계속 바뀝니다.'),
 ('support','상담원이 안내한 절차를 다시 확인하고 싶습니다.'),
 ('billing','반품 건의 환불이 아직 완료되지 않았습니다.'),
 ('delivery','택배 수령 확인이 잘못 표시됩니다.'),
 ('support','응대 내용을 문자로 보내 주실 수 있나요?'),
 ('billing','청구 명세서에 세금 항목이 두 번 나옵니다.'),
 ('delivery','배송 단계별 알림을 받고 싶습니다.'),
 ('support','상담 연결이 반복해서 끊어집니다.'),
 ('billing','결제 후 영수증을 찾을 수 없습니다.'),
 ('delivery','도착 예정 안내가 주문마다 다르게 보입니다.'),
 ('support','문의 처리 상태를 확인하고 싶습니다.'),
 ('billing','환불 금액이 안내받은 금액보다 적습니다.'),
 ('delivery','택배 상자의 내용물이 누락됐습니다.'),
 ('support','응대가 지연되는 이유를 알려 주세요.'),
 ('billing','청구 날짜를 다음 달로 변경할 수 있나요?'),
]


def cases(period):
    if period not in PERIODS:
        raise ValueError('unsupported PoC period')
    rows=_WEEK1 if period=='week1' else _WEEK2
    return [{'record_id':f'POC-{period.upper()}-{i:02d}','source_version':PERIODS[period],
             'body':body,'expected_category':category} for i,(category,body) in enumerate(rows,1)]


def records(period):
    return [{k:v for k,v in row.items() if k!='expected_category'} for row in cases(period)]
