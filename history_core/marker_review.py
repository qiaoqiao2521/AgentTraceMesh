"""Deterministic routing candidates from redacted, public user requests only."""
import re

# Ordinary routing cues and names users actually use when reminding an agent.
_STUDIO = {
    'studio-production-workflow': (r'试衣间|换装.{0,24}运镜|插件兼容|工作室.{0,20}(?:配方|成片)|服装.{0,12}运镜', r'工作室总控|工作室工作流'),
    'blender-mesh-xray': (r'改模|改衣|改姿|网格切片|检查.{0,12}(?:穿模|壁厚)|mesh.{0,20}(?:clipping|wall thickness)', r'网格[XＸ]光|网格切片|mesh x.?ray'),
    'cinema-previs-blockout': (r'拉片|灰模预演|(?:视频|镜头).{0,24}灰模|灰模.{0,24}(?:镜头|预演)', r'灰模.{0,8}(?:技能|skill)'),
    'codex-director': (r'指挥.{0,8}codex|让.{0,6}codex.{0,8}(?:审|干活)|独立审阅|工作流.{0,24}最小改动', r'codex\s*调用\s*(?:skill|技能)|指挥\s*codex'),
    'perspective-illusion': (r'isotruth-3d|透视错觉', r'视觉错觉\s*(?:skill|技能)|透视错觉'),
    'blender-arp-rig-locomotion': (r'自动绑骨|绑定.{0,16}(?:步态|走路)|ARP.{0,12}(?:rig|步态)', r'ARP\s*(?:skill|技能)'),
    'music-to-3d-dance': (r'(?:纯音乐|只有音乐|这首歌).{0,24}(?:舞步|编舞|卡点)|琴键粒子', r'音乐编舞'),
    'per-segment-tts-alignment': (r'逐段.{0,12}(?:配音|音画)对齐', r'分段配音'),
    'wechat-format': (r'公众号.{0,10}排版', r'公众号排版'),
}
_ALL = {
    'tracemesh-recall': (r'检索.{0,6}对话|看看之前怎么做|找回.{0,8}会话', r'历史检索'),
    'agy-slot': (r'(?:colab|agy|codex).{0,8}(?:切槽|换号|查槽)', r'槽位管理'),
}
_REMINDER = re.compile(r'提醒|为什么.{0,10}(?:没|不)|怎么.{0,8}(?:没|不)|一直没|别忘|忘了|记得用|对了.{0,15}有|'
                       r'\b(?:remember|remind|forgot|why (?:didn.t|haven.t|not))\b', re.I)
_EXAMPLE = re.compile(r'^\s*(?:>|Ran |Analyzed |\[\[tracemesh:|示例[：:]|Example:)', re.I)
_INVENTORY = re.compile(r'(?:盘点|清单|inventory|统计|审计).{0,20}(?:技能|skills?|调用)|(?:技能|skills?).{0,12}(?:盘点|库存|审计)', re.I)
_AUTHORING = re.compile(r'(?:新建|创建|编写|做一个|尚未发布|还没发布|create|write).{0,25}(?:skill|技能)', re.I)
_REVIEW = re.compile(r'仅复盘|只复盘|只是复盘|不要.{0,8}(?:制作|修改)|仅供参考')
_DIRECT = re.compile(r'(?:请|先|应该|必须|记得|怎么|为什么)?.{0,4}(?:调用|使用|加载|读取)|\b(?:use|load|read|invoke)\b', re.I)


def route_requests(text, preset):
    if text and text.lstrip().startswith('<USER_REQUEST>'):
        body, closed, _ = text.lstrip()[len('<USER_REQUEST>'):].partition('</USER_REQUEST>')
        text = body if closed else ''
    if not text or '<INSTRUCTIONS>' in text or '# AGENTS.md' in text or 'Director Brief' in text:
        return
    # Fenced examples, quotations and pasted tool histories are not instructions.
    parts = re.split(r'^\s*(?:\x60{3,}|~{3,}).*$', text, flags=re.M)
    public = '\n'.join(line for part in parts[::2] for line in part.splitlines() if not _EXAMPLE.match(line))
    if not public.strip():
        return
    exclusion = ('skill_authoring_or_unpublished' if _AUTHORING.search(public) else
                 'inventory_or_audit' if _INVENTORY.search(public) else
                 'retrospective_only' if _REVIEW.search(public) else None)
    rules = {**_STUDIO, **_ALL} if preset == 'all' else _STUDIO
    for name, (intent, alias) in rules.items():
        named = re.search(re.escape(name) + '|' + alias, public, re.I)
        hit = named or re.search(intent, public, re.I)
        if not hit:
            continue
        nearby = public[max(0, hit.start()-65):hit.end()+65]
        reminded = bool(named and _REMINDER.search(nearby))
        explicit = bool(named and (reminded or _DIRECT.search(nearby)))
        if not explicit and not exclusion and not re.search(intent, public, re.I):
            continue
        yield name, dict(matched=hit[0][:120], reminder=reminded, explicit=explicit, exclusion=exclusion)


def assess(skill, requests, event, observable):
    first, reminder = requests['first'], requests.get('reminder')
    def position(item):
        return (item['activity_ts_ms'], item['raw_ref'].get('line_no', item.get('message_index') or 0))
    excluded = first['exclusion']
    if excluded:
        status, reason = 'excluded', excluded
    elif not observable:
        status, reason = 'unknown', 'incomplete_recording_coverage'
    elif reminder and event and position(event) > position(reminder):
        status, reason = 'late', 'first_recorded_event_after_user_reminder'
    elif event:
        status, reason = 'timely', 'recorded_without_prior_reminder'
    elif requests['explicit']:
        status, reason = 'expected_unseen', 'explicitly_requested_no_recorded_event'
    else:
        status, reason = 'unknown', 'route_matched_historical_availability_unverified'
    return dict(skill=skill, status=status, reason=reason, first_request=first, reminder=reminder,
                first_event=event, candidate='expected_unseen' if not event and not excluded else None)
