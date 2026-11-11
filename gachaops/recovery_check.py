"""User-requested recovery. Verify evidence and fresh identities; never kill or run games."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from .execution_lock import ExecutionLock, ExecutionBusy
from .recovery import read_state, save, RecoveryRequired
from .windows_observation import inspect_process_identity, find_matching_processes


class InspectionBlocked(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InspectionBlocked(message)


def read_json(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    require(isinstance(data, dict), '运行证据格式无效，需要人工检查。')
    return data


def scoped_path(path, parent):
    path = Path(path).resolve()
    require(path.is_relative_to(parent.resolve()), '恢复证据路径不属于本次运行。')
    return path


def chain_for(workspace, journal):
    runs = workspace / 'runs'
    if journal.get('chain_summary'):
        paths = [scoped_path(journal['chain_summary'], runs)]
    else:
        paths = list(runs.glob('gui-*/assistants-*/summary.json')) + list(runs.glob('assistants-*/summary.json'))
    matches = []
    for path in paths:
        try:
            chain = read_json(path)
        except (OSError, ValueError):
            continue
        if chain.get('run_id') == journal['run_id']:
            matches.append((scoped_path(path, runs), chain))
    require(len(matches) == 1, '找不到唯一的本轮报告，需要人工检查；不会解除隔离。')
    return matches[0]


def valid_identity(row):
    return (isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] > 0
            and type(row.get('creation_ticks')) is int and row['creation_ticks'] > 0
            and isinstance(row.get('image'), str) and Path(row['image']).is_absolute())


def recovery_instructions(workspace, names=None):
    """Read-only context and actions; never inspect or change live processes here."""
    context = ''
    try:
        journal = read_state(Path(workspace) / 'state')
        if journal.get('run_id'):
            context = '\n隔离运行：' + journal['run_id']
            _, chain = chain_for(Path(workspace), journal)
            failed = [row for row in chain.get('assistants', []) if isinstance(row, dict)
                      and row.get('status') != 'disabled' and row.get('cleanup_verified') is False]
            labels = [(names or {}).get(row.get('id'), str(row.get('id') or '未知助手')) for row in failed]
            if labels:
                context += '\n清理待核验：' + '、'.join(labels)
    except (OSError, ValueError, KeyError, TypeError, RecoveryRequired):
        pass
    return ('上一轮清理未确认，当前轮未启动。' + context + '\n\n恢复步骤：\n'
            '1. 退出上一轮相关助手、游戏和启动器。\n'
            '2. 以管理员权限打开同一个 GachaOps。\n'
            '3. 在“运行情况”或当前“查看原因”窗口点击“检查并恢复运行资格”。\n'
            '程序会核对进程身份、清理证据及当前残留；不会启动或停止游戏。\n'
            '4. 显示恢复成功后，再手动点击开始运行。\n'
            '如果核验失败，请按具体进程或权限提示处理后再检查；若提示证据缺失或身份无法确认，'
            '请点击“导出诊断包”交给维护者核验，无需反复点击开始。不要删除或修改隔离记录。')


def recover_workspace(workspace):
    workspace = Path(workspace).resolve()
    state = workspace / 'state'
    proof = {'checked_at': datetime.now(timezone.utc).isoformat(), 'process_checks': []}
    evidence = None
    try:
        with ExecutionLock():
            journal = read_state(state)
            if journal['phase'] in {'unused', 'idle'}:
                return {'status': 'already_ready', 'message': '当前未被隔离，可以手动开始运行。'}
            require(journal['phase'] == 'quarantined', '运行尚未结束或状态未确认，请先等待运行结束。')
            chain_path, chain = chain_for(workspace, journal)
            require(chain.get('status') == 'stopped_cleanup_unconfirmed', '本轮缺少完整收尾报告，需要人工检查。')
            evidence = chain_path.parent.parent / 'monitor-evidence' / ('recovery-' + uuid.uuid4().hex)
            evidence.mkdir(parents=True)
            save(evidence / 'original-journal.json', journal)
            save(evidence / 'original-chain.json', chain)
            proof['run_id'] = journal['run_id']
            attempts = []
            require(isinstance(chain.get('assistants'), list), '助手报告格式无效。')
            for result in chain['assistants']:
                require(isinstance(result, dict), '助手结果格式无效。')
                if result.get('status') != 'disabled':
                    history = result.get('attempts') or [result]
                    require(isinstance(history, list) and all(isinstance(r, dict) for r in history),
                            '尝试记录格式无效。')
                    attempts.extend(history)
            unconfirmed = [r for r in attempts if r.get('cleanup_verified') is not True]
            require(len(unconfirmed) == 1, '未确认清理的节点不唯一，需要人工检查。')
            failed = unconfirmed[0]
            if failed.get('node_summary'):
                node_path = scoped_path(failed['node_summary'], chain_path.parent)
            else:
                candidates = [p for p in chain_path.parent.glob('node-*/summary.json')
                              if read_json(p).get('id') == failed.get('id')]
                require(len(candidates) == 1, '旧报告缺少唯一的节点证据，需要人工检查。')
                node_path = scoped_path(candidates[0], chain_path.parent)
            node = read_json(node_path)
            save(evidence / 'original-node.json', node)
            require(node.get('run_id') == journal['run_id'] and node.get('id') == failed.get('id')
                    and node.get('status') == failed.get('status'), '节点与本轮身份不一致。')
            require(node.get('owned_job_cleanup_verified') is True, '原运行没有确认本轮 Job 清理成功，不能恢复。')
            remaining = node.get('remaining_processes')
            require(isinstance(remaining, dict) and remaining.get('status') in {'observed', 'uncertain'}
                    and isinstance(remaining.get('uncertain'), list)
                    and (bool(remaining['uncertain']) == (remaining['status'] == 'uncertain'))
                    and isinstance(remaining.get('matches'), list)
                    and all(valid_identity(r) for r in remaining['matches']),
                    '原运行的残留观察尚未确认，不能恢复。')
            historical = list(remaining['matches'])
            for key in ('borrowed_game', 'unowned_processes'):
                rows = node.get(key, [])
                require(isinstance(rows, list) and all(valid_identity(r) for r in rows),
                        '原运行的外部进程身份缺失，不能恢复。')
                historical.extend(rows)
            owned = node.get('ownership_observations') or []
            require(owned and all(valid_identity(r) and r.get('in_owned_job') is True for r in owned)
                    and len({r['pid'] for r in owned}) == len(owned)
                    and journal.get('owned_processes') == owned, '本轮进程归属证据缺失或不一致。')
            targets = node.get('observation_targets') or []
            require(isinstance(targets, list), '本轮扫描路径格式无效。')
            # Older OK adapters saved None for an unused optional launcher.
            # Required game/runner paths are still checked explicitly below.
            targets = [p for p in targets if p is not None]
            require(targets and all(isinstance(p, str) and Path(p).is_absolute() for p in targets),
                    '旧记录缺少本轮扫描路径，需要人工检查。')
            game = node.get('game_path')
            require(isinstance(node.get('argv'), list) and node['argv'], '助手启动记录缺失。')
            require(isinstance(game, str) and Path(game).is_absolute() and game in targets
                    and node['argv'][0] in targets, '本轮助手或游戏扫描路径未保存完整。')
            # These candidates had no creation identity. They may only be cleared
            # by a fresh confirmed absence, never by assuming a live PID was reused.
            unknown = remaining['uncertain']
            candidate_names = {Path(p).name.casefold() for p in targets + [
                row['image'] for row in owned + historical]}
            require(all(isinstance(row, dict) and type(row.get('pid')) is int and row['pid'] > 0
                        and isinstance(row.get('name'), str) and '/' not in row['name']
                        and '\\' not in row['name'] and row['name'].casefold() in candidate_names
                        and ((type(row.get('error')) is int and row['error'] > 0)
                             or (isinstance(row.get('error'), str) and bool(row['error'].strip())))
                        for row in unknown)
                    and len({row['pid'] for row in unknown}) == len(unknown),
                    '历史不确定进程记录缺失或与本轮目标不符，不能安全核验。')
            supervisor_pid = journal.get('supervisor_pid')
            require(type(supervisor_pid) is int and supervisor_pid > 0, '监督进程身份缺失。')
            identities = [(supervisor_pid, journal.get('supervisor_identity'), 'GachaOps 运行器')] + [
                (r['pid'], r, r['image']) for r in owned + historical] + [
                (r['pid'], None, r['name']) for r in unknown]
            for pid, old, label in identities:
                check = {'pid': pid, 'old_identity': old, 'label': label}
                proof['process_checks'].append(check)
                try:
                    current = inspect_process_identity(pid)
                except OSError as exc:
                    check['error'] = str(exc)
                    permission = ('请以管理员权限重新打开 GachaOps 后再检查。'
                                  if isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) == 5 else
                                  '请按进程检查错误处理后再检查。')
                    raise InspectionBlocked(f'无法核验进程 PID {pid}（{label}）：{exc}。{permission}') from exc
                check['live_identity'] = current
                if current is not None:
                    require(valid_identity(current) and current['pid'] == pid, f'无法核验进程 {pid} 的身份。')
                    # Two different blocked causes need different user actions:
                    # a live process to exit, versus a record we cannot compare.
                    require(valid_identity(old),
                            f'进程 PID {pid}（{label}）仍在运行，但没有可比对的历史身份，'
                            '无法排除 PID 复用；请先退出相关程序后重新检查，仍失败请导出诊断包。')
                    require(current['creation_ticks'] != old['creation_ticks'],
                            f'进程 PID {pid}（{label}）仍在运行，请先退出相关程序后重新检查。')
            # Include renamed updater executables within this installation. System
            # helpers are checked by their old identity, not by global filename.
            installation = Path(node['cwd']).resolve()
            targets = list(dict.fromkeys(targets + [r['image'] for r in historical] + [
                r['image'] for r in owned
                if Path(r['image']).resolve().is_relative_to(installation)]))
            proof['targets'] = targets
            observed = find_matching_processes(targets)
            proof['matching_processes'] = observed
            details = '\n'.join(f"PID {r.get('pid', '?')}：{r.get('image') or r.get('name') or r.get('error')}"
                                for r in observed.get('matches', []) + observed.get('uncertain', []))
            require(observed == {'status': 'observed', 'matches': [], 'uncertain': []},
                    '仍发现相关进程，或进程访问权限不足；请退出相关助手、游戏和启动器，'
                    '以管理员权限重新打开 GachaOps 后再检查。\n' + details)
            require(read_state(state) == journal, '运行状态已改变，请重新检查。')
            save(evidence / 'fresh-inspection.json', dict(proof, status='passed'))
            restored = dict(journal, phase='idle', cleanup_verified=True,
                ended_at=proof['checked_at'], outcome=chain['status'],
                recovery_reason='user_requested_verified_cleanup_and_fresh_no_residuals',
                recovery_inspection=str(evidence / 'fresh-inspection.json'))
            save(evidence / 'planned-journal.json', restored)
            # Commit last: any earlier audit failure must leave quarantine intact.
            save(state / 'execution.json', restored)
            return {'status': 'recovered', 'run_id': journal['run_id'], 'evidence': str(evidence),
                    'message': '核对通过，运行资格已恢复。请手动开始运行；原失败记录已保留。'}
    except (ExecutionBusy, RecoveryRequired, OSError, ValueError, KeyError, TypeError) as exc:
        if evidence is not None:
            try:
                save(evidence / 'fresh-inspection.json', dict(proof, status='blocked', error=str(exc)))
            except OSError:
                pass
        message = '已有 GachaOps 运行在进行，请等待结束后再检查。' if isinstance(exc, ExecutionBusy) else str(exc)
        if isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) == 5:
            message += '\n请以管理员权限重新打开 GachaOps 后再检查。'
        return {'status': 'blocked', 'message': '未能恢复运行资格：' + message +
                '\n若处理后仍失败，请在“查看原因”点击“导出诊断包”交给维护者核验；'
                '无需反复点击开始，不要删除或修改隔离记录。',
                'evidence': str(evidence) if evidence else None}
