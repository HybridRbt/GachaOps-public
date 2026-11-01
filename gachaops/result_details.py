"""User-facing result details, including verified offline enrichment of old reports."""
import json
from pathlib import Path

from .desktop_model import NAMES, STATUS
from . import march7


def details_for(result, summary_path=None):
    completion = dict(result.get('completion') or {})
    source = None
    supplemental = False
    verification_error = False
    if summary_path:
        try:
            chain_path = Path(summary_path)
            chain = json.loads(chain_path.read_text(encoding='utf-8-sig'))
            candidates = sorted(chain_path.parent.glob('node-*/summary.json'))
            # Multiple attempts share an assistant ID and run ID. The explicit
            # attempt path must also belong to this chain, never a foreign file.
            if result.get('node_summary'):
                wanted = Path(result['node_summary']).resolve()
                candidates = [path for path in candidates if path.resolve() == wanted]
                if not candidates:
                    verification_error = True
            for path in candidates:
                node = json.loads(path.read_text(encoding='utf-8-sig'))
                if (not chain.get('run_id') or node.get('id') != result.get('id')
                        or node.get('run_id') != chain.get('run_id')):
                    continue
                source = path
                if result.get('id') == 'genshin':
                    output = path.parent / 'bettergi-output.log'
                    if output.is_file():
                        source = output
                if result.get('id') == 'honkai-star-rail':
                    launch = json.loads((path.parent / 'march7-launch.json').read_text(encoding='utf-8'))
                    observed = march7.observe(march7.Cursor(str(path.parent), launch['token'],
                        launch['argv_candidate'][0]), node.get('exit_code'), node.get('root_pid'),
                        node.get('ownership_observations'))
                    if observed.get('evidence') == 'owned_native_full_run_and_stop_completed':
                        completion = observed
                        source = path.parent / 'march7-output.log'
                        supplemental = True
                    else:
                        verification_error = True
                break
        except (OSError, ValueError, KeyError, TypeError):
            verification_error = True
    name = NAMES.get(result.get('id'), result.get('id') or '本轮')
    lines = [f'{name} · {STATUS.get(result.get("status"), "结果待确认")}']
    if result.get('duration_seconds') is not None:
        lines.append(f'用时：{round(result["duration_seconds"])} 秒')
    if result.get('round'):
        lines.append(f"本次结果：第 {result['round']} 轮")
    failed = completion.get('failed_applications') or []
    if failed:
        lines += ['', '未完成任务：', *[f'• {task}' for task in failed]]
    if completion.get('daily_training_incomplete'):
        lines += ['', '每日实训未完成' + (
            f'（最后记录分数 {completion["daily_training_score"]}）'
            if completion.get('daily_training_score') else '')]
    messages = list(completion.get('errors') or []) + list(completion.get('failure_warnings') or [])
    if result.get('error'):
        messages.append(str(result['error']))
    if messages:
        lines += ['', '助手或运行器报告：', *map(str, messages)]
    reason = completion.get('reason') or result.get('reason')
    if reason:
        lines += ['', '结果未确认的记录：', str(reason)]
    corruption = completion.get('log_corruption')
    if corruption:
        lines += ['', '原生日志损坏：'
                  f'已审计跳过 {corruption.get("skipped_lines", 0)} 行非关键记录，'
                  f'{corruption.get("blocked_lines", 0)} 行因证据不明而阻止确认。']
        if corruption.get('audit_path'):
            lines.append(f'审计记录：{corruption["audit_path"]}')
    unsuccessful = result.get('status') not in {'success', 'disabled', 'running', 'starting', 'cancelled', 'nothing_enabled'}
    unknown_cause = unsuccessful and (result.get('cause_unknown') is True or (
        not messages and (not reason or completion.get('status') == 'completion_unconfirmed')))
    if unknown_cause:
        lines += ['', '具体原因未确认：助手未提供可核实的原因。']
        lines += ['', '可手动检查以下可能情况：',
                  '• 助手更新后，可能需要手动打开一次并确认更新提示。',
                  '• 游戏可能正在更新，或等待登录、公告等确认。',
                  '• 助手可能有其他弹窗，或当前配置需要处理。',
                  '以上是可能原因，并非已确认的诊断。',
                  '处理后退出助手，再尝试运行 GachaOps；清理未确认时，仍需先核对并解除隔离。']
    recovered = completion.get('recovered_warnings') or []
    if recovered:
        lines += ['', '已恢复的警告（不作为最终失败依据）：', *map(str, recovered)]
    evidence = completion.get('task_failure_evidence') or []
    if evidence:
        lines += ['', '任务原始记录：', *map(str, evidence)]
    if supplemental:
        lines += ['', '已核验本轮原生输出并补充解析；历史报告保持原样。']
    if verification_error:
        lines += ['', '本轮原生记录无法核验，仅展示已保存的报告。']
    attempts = result.get('attempts') or []
    if len(attempts) > 1 or (attempts and result.get('status') in {'running', 'starting'}):
        lines += ['', '历次尝试（保留每次结果）：']
        for attempt in attempts:
            lines += ['', f"—— 第 {attempt['round']} 轮 ——",
                      details_for({key: value for key, value in attempt.items() if key != 'attempts'},
                                  summary_path)['text']]
    lines += ['', '可先在助手中检查这些任务及预设；本页面不会重试或修改游戏。']
    return {'text': '\n'.join(lines), 'source': source, 'completion': completion}
