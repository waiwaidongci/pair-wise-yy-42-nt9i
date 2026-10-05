from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])

# 派工（火场撤离调度）相关角色与状态
DISPATCH_ROLES=set(['field_commander', 'incident_commander', 'logistics'])
MEMBER_ROLES=set(['field_commander', 'incident_commander', 'logistics'])
TASK_AREA_CREATE_ROLES=set(['field_commander', 'incident_commander'])
SIGN_OFF_ROLES=set(['field_commander', 'incident_commander'])
WIND_ROLES=set(['field_commander', 'incident_commander', 'logistics'])
SUPPLY_ROLES=set(['field_commander', 'logistics'])
CLOSE_CONFIRM_ROLES=set(['incident_commander'])

WIND_LEVELS=list(range(0, 13))  # 蒲福风级 0-12
DISPATCH_STATES=['pending', 'in_progress', 'completed', 'cancelled', 'pending_coordination']
# 占用态：同一队员同时只能被一张活动派工占用
ACTIVE_DISPATCH_STATES=['pending', 'in_progress']
# 任务区状态：open 开放 / signed 已签认待确认 / closed 已关闭
TASK_AREA_STATES=['open', 'signed', 'closed']
MEMBER_STATES=['on_duty', 'returned']  # 未归队 / 已归队
MEMBER_ON_DUTY='on_duty'; MEMBER_RETURNED='returned'

def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

def normalize_wind_level(value):
    """校验风级为 0-12 的整数（蒲福风级）。"""
    if isinstance(value, bool): raise ValidationError("wind_level必须是整数")
    try: level=int(value)
    except (TypeError,ValueError): raise ValidationError("wind_level必须是整数")
    if level not in WIND_LEVELS: raise ValidationError("wind_level必须在0到12之间")
    return level

def dispatch_priority(wind_level):
    """派工优先级随风级升高；风级越大越紧急，区间 1-10。"""
    level=normalize_wind_level(wind_level)
    return max(1,min(10,level))

def recalculated_dispatch(wind_level):
    """风级变化后，未开工派工随新条件重算：更新风级快照与优先级。"""
    level=normalize_wind_level(wind_level)
    return {"wind_level": level, "priority": dispatch_priority(level)}

def is_active_dispatch(status):
    return status in ACTIVE_DISPATCH_STATES

def validate_dispatch_state(status):
    if status not in DISPATCH_STATES: raise ValidationError("未知派工状态")
    return status

def validate_task_area_state(status):
    if status not in TASK_AREA_STATES: raise ValidationError("未知任务区状态")
    return status

def close_checklist_blockers(unreturned_members, incomplete_supplies, pending_coordination):
    """关闭任务区前必须列清的三类事项：未归队队员、未齐物资、待协调占用。"""
    blockers=[]
    if unreturned_members:
        names="、".join(m.get("name","?") for m in unreturned_members)
        blockers.append(f"未归队队员{len(unreturned_members)}人：{names}")
    if incomplete_supplies:
        names="、".join(s.get("name","?") for s in incomplete_supplies)
        blockers.append(f"未齐物资{len(incomplete_supplies)}项：{names}")
    if pending_coordination:
        refs="、".join(d.get("order_no","?") for d in pending_coordination)
        blockers.append(f"待协调占用{len(pending_coordination)}项：{refs}")
    return blockers
