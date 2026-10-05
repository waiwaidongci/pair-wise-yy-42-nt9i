from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
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

# ---- 火场撤离调度 ----
ZONE_ENTITY='任务区'; DISPATCH_ENTITY='派工单'; ZONE_STATES=['open','closed']
DISPATCH_STATES=['planned','in_progress','completed','closed']
DISPATCH_TRANSITIONS={'planned': ['in_progress'], 'in_progress': ['completed'], 'completed': ['closed'], 'closed': []}
DISPATCH_TRANSITION_ROLES={'in_progress': ['field_commander'], 'completed': ['field_commander'], 'closed': ['incident_commander']}
ZONE_CREATE_ROLES=set(['incident_commander']); ZONE_CLOSE_ROLES=set(['incident_commander'])
DISPATCH_CREATE_ROLES=set(['field_commander', 'incident_commander']); REPORT_ROLES=set(['field_commander', 'incident_commander'])
RESOLVE_ROLES=set(['incident_commander']); MATERIAL_ROLES=set(['logistics'])
REPORT_KINDS=['wind', 'breakpoints', 'location']; MEMBER_STATUSES=['occupied', 'pending_coordination', 'returned', 'released']
def dispatch_plan(wind_level,breakpoints,member_count):
    points=breakpoints or []
    risk=round(min(10.0,wind_level*1.2+len(points)*0.5),2)
    egress=max(10,int(120-wind_level*8-len(points)*2))
    min_crew=max(2,(len(points)+1)//2+(1 if wind_level>=8 else 0))
    return {"wind_level":wind_level,"breakpoint_count":len(points),"risk_score":risk,"egress_window_minutes":egress,"min_crew":min_crew,"crew_ok":member_count>=min_crew}
def can_transition_dispatch(current,target): return target in DISPATCH_TRANSITIONS.get(current,[])
def validate_dispatch_transition(current,target):
    if current not in DISPATCH_STATES or target not in DISPATCH_STATES: raise ValidationError("未知派工状态")
    if not can_transition_dispatch(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def role_for_dispatch_transition(target): return set(DISPATCH_TRANSITION_ROLES.get(target,[]))
def closure_clear(checklist):
    return not (checklist["unreturned_members"] or checklist["incomplete_materials"] or checklist["pending_coordinations"])
def format_closure_blockers(checklist):
    parts=[]
    members=[row["member_name"] for row in checklist["unreturned_members"]]
    if members: parts.append("未归队队员:"+",".join(members))
    materials=[f"{row['name']}(缺{row['missing_qty']:g})" for row in checklist["incomplete_materials"]]
    if materials: parts.append("未齐物资:"+",".join(materials))
    pending=[row["member_name"] for row in checklist["pending_coordinations"]]
    if pending: parts.append("待协调占用:"+",".join(pending))
    return "；".join(parts)
