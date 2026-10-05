from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; ROLES=['field_commander', 'incident_commander', 'logistics', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def require_wind_level(value,field="wind_level"):
    number=require_number(value,field,0.0)
    if number>17: raise ValidationError(f"{field}必须在0-17级之间")
    return number
def normalize_breakpoints(value,field="breakpoints"):
    if not isinstance(value,list) or not value: raise ValidationError(f"{field}必须是非空折点列表")
    if len(value)>500: raise ValidationError(f"{field}不能超过500个折点")
    points=[]
    for point in value:
        if isinstance(point,dict): lat,lng=point.get("lat"),point.get("lng")
        elif isinstance(point,(list,tuple)) and len(point)==2: lat,lng=point[0],point[1]
        else: raise ValidationError(f"{field}折点必须是[纬度,经度]或含lat/lng的对象")
        lat=require_number(lat,"lat",-90.0); lng=require_number(lng,"lng",-180.0)
        if lat>90: raise ValidationError("lat必须在-90到90之间")
        if lng>180: raise ValidationError("lng必须在-180到180之间")
        points.append({"lat":lat,"lng":lng})
    return points
def require_name_list(value,field="members",max_items=200):
    if not isinstance(value,list) or not value: raise ValidationError(f"{field}必须是非空名单列表")
    if len(value)>max_items: raise ValidationError(f"{field}不能超过{max_items}人")
    names=[]; seen=set()
    for item in value:
        name=require_text(item,field,100)
        if name not in seen: seen.add(name); names.append(name)
    return names
