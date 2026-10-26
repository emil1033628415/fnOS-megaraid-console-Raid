#!/bin/bash
# tools/prepublish-check.sh -- 发布前闸门：代码完整性 + 敏感信息。
#
# 每次发布（打 fpk、推 GitHub、发 Release）之前跑一遍；CI 也会在每次 push/PR 时跑
# （CI 模式会跳过只在本机存在的 deny 列表与 fnpack 打包检查）。
#
#   ./tools/prepublish-check.sh          # 本机完整检查
#   ./tools/prepublish-check.sh --ci     # CI 模式（不依赖本机私有文件）
#
# 退出码：0 全部通过；1 有失败项；2 脚本自身环境不满足。
set -uo pipefail

CI_MODE=0
[ "${1:-}" = "--ci" ] && CI_MODE=1

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 2

FAIL=0
WARN=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; WARN=$((WARN + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# deny 列表放在仓库之外，避免把真机标识写进要发布的代码
DENY="${PREPUBLISH_DENY:-/vol2/@appshare/fn-deepseek-harness/Deepseek/.prepublish-deny}"

# --------------------------------------------------------------------------- #
section "1. 必需文件"
# --------------------------------------------------------------------------- #
REQUIRED=(
    manifest ICON.PNG ICON_256.PNG LICENSE README.md CHANGELOG.md
    SECURITY.md THIRD_PARTY_NOTICES.md .gitignore .gitattributes
    app/server/raidwatchd.py app/server/raidcore.py app/server/pam_verify.py
    app/ui/app.js app/ui/index.html app/ui/style.css app/ui/config
    app/ui/images/icon_256.png app/ui/images/icon_64.png
    app/config/privilege app/config/resource
    config/privilege config/resource
    cmd/main cmd/install_init cmd/install_callback cmd/uninstall_init
    cmd/uninstall_callback cmd/upgrade_init cmd/upgrade_callback
    cmd/config_init cmd/config_callback
    tests/test_regression.py tools/hotpatch.sh
)
missing=0
for f in "${REQUIRED[@]}"; do
    [ -f "$f" ] || { fail "缺少 $f"; missing=$((missing + 1)); }
done
[ "$missing" -eq 0 ] && pass "${#REQUIRED[@]} 个必需文件齐全"

# --------------------------------------------------------------------------- #
section "2. 可执行位"
# --------------------------------------------------------------------------- #
if command -v git >/dev/null && git rev-parse --git-dir >/dev/null 2>&1; then
    # 按清单校验而不是数个数：新增脚本时忘记 chmod，这里会直接指出来。
    EXECS=(cmd/main cmd/config_init cmd/config_callback
           cmd/install_init cmd/install_callback
           cmd/uninstall_init cmd/uninstall_callback
           cmd/upgrade_init cmd/upgrade_callback
           tools/hotpatch.sh tools/prepublish-check.sh)
    bad_exe=0
    for f in "${EXECS[@]}"; do
        git ls-files -s "$f" | grep -q '^100755' || { fail "$f 在 git 索引里不是 100755（CI 会 Permission denied）"; bad_exe=$((bad_exe + 1)); }
        [ -x "$f" ] || { fail "$f 在磁盘上不可执行"; bad_exe=$((bad_exe + 1)); }
    done
    [ "$bad_exe" -eq 0 ] && pass "${#EXECS[@]} 个脚本均为 100755（索引与磁盘一致）"
    # 反向检查：不该有别的文件被误标为可执行
    extra_exe=$(git ls-files -s | awk '$1=="100755"{print $4}' | grep -vxF -f <(printf '%s\n' "${EXECS[@]}") || true)
    [ -z "$extra_exe" ] || warn "这些文件也被标成可执行，确认是否本意：$extra_exe"
else
    warn "不在 git 工作区，跳过索引权限位检查"
fi

# --------------------------------------------------------------------------- #
section "3. 版本一致性"
# --------------------------------------------------------------------------- #
ver_manifest=$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' manifest | head -1)
ver_code=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' app/server/raidwatchd.py | head -1)
ver_log=$(sed -n 's/^## \[\([0-9.]*\)\].*/\1/p' CHANGELOG.md | head -1)
[ -n "$ver_manifest" ] && pass "manifest: $ver_manifest" || fail "manifest 里读不到 version"
[ "$ver_code" = "$ver_manifest" ] && pass "后端 VERSION: $ver_code" \
    || fail "后端 VERSION=$ver_code 与 manifest=$ver_manifest 不一致"
[ "$ver_log" = "$ver_manifest" ] && pass "CHANGELOG 最新条目: $ver_log" \
    || fail "CHANGELOG 最新条目=$ver_log 与 manifest=$ver_manifest 不一致"
grep -qF "**${ver_manifest} 亮点**" README.md \
    && pass "README 含 ${ver_manifest} 亮点" \
    || warn "README 里没有「**${ver_manifest} 亮点**」段落（可选）"

# --------------------------------------------------------------------------- #
section "4. 语法与测试"
# --------------------------------------------------------------------------- #
if PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY' 2>/dev/null
import ast, glob, sys
for p in ["app/server/raidcore.py", "app/server/raidwatchd.py", "app/server/pam_verify.py"]:
    ast.parse(open(p, encoding="utf-8").read(), filename=p)
PY
then pass "Python 源码可解析"; else fail "Python 源码有语法错误"; fi

if command -v node >/dev/null 2>&1; then
    node --check app/ui/app.js >/dev/null 2>&1 && pass "app.js 语法 OK" || fail "app.js 语法错误"
else
    warn "未安装 node，跳过前端语法检查"
fi

if bash -n cmd/* tools/*.sh 2>/dev/null; then pass "shell 脚本语法 OK"; \
else fail "shell 脚本语法错误"; fi

if PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests >/tmp/prepub-tests.log 2>&1; then
    pass "单元测试通过（$(grep -oE 'Ran [0-9]+ tests' /tmp/prepub-tests.log | tail -1)）"
else
    fail "单元测试失败，详见 /tmp/prepub-tests.log"
fi

# --------------------------------------------------------------------------- #
section "5. 不该进仓库的文件"
# --------------------------------------------------------------------------- #
if command -v git >/dev/null && git rev-parse --git-dir >/dev/null 2>&1; then
    junk=$(git ls-files | grep -E '\.fpk$|__pycache__|\.pyc$|\.log$|\.ghtoken$|\.DS_Store$|^\.git/' || true)
    [ -z "$junk" ] && pass "版本控制中没有构建产物 / 日志 / 凭据文件" \
        || fail "这些文件被纳入了版本控制：$junk"
    dirty=$( { git diff --name-only; git ls-files --others --exclude-standard; } | wc -l )
    [ "$dirty" -eq 0 ] && pass "工作区干净（无未暂存/未跟踪文件）" \
        || warn "工作区有 $dirty 个文件未暂存或未跟踪"
else
    junk=$(find . -name '*.fpk' -o -name '__pycache__' -o -name '*.log' -o -name '.ghtoken' | head -5)
    [ -z "$junk" ] && pass "目录中没有构建产物 / 日志 / 凭据文件" || fail "发现残留：$junk"
fi

# --------------------------------------------------------------------------- #
section "6. 敏感信息：密钥与凭据"
# --------------------------------------------------------------------------- #
PATTERNS=(
    'sk-[A-Za-z0-9]{16,}'                        # DeepSeek/OpenAI 风格
    'ghp_[A-Za-z0-9]{20,}'                       # GitHub classic PAT
    'github_pat_[A-Za-z0-9_]{20,}'               # GitHub fine-grained PAT
    'AKIA[0-9A-Z]{16}'                           # AWS
    '-----BEGIN [A-Z ]*PRIVATE KEY'              # 私钥
    'xox[baprs]-[A-Za-z0-9-]{10,}'               # Slack
    'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'  # JWT
)
hits=0
for pat in "${PATTERNS[@]}"; do
    found=$(grep -rIlE "$pat" . --exclude-dir=.git --exclude-dir=node_modules 2>/dev/null || true)
    [ -n "$found" ] && { fail "命中密钥模式 /$pat/：$found"; hits=$((hits + 1)); }
done
[ "$hits" -eq 0 ] && pass "无密钥 / 令牌 / 私钥模式"

# 口令、token 之类的硬编码赋值（排除变量声明与空值）
assigned=$(grep -rInE '(password|passwd|secret|token|api[_-]?key|auth)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{6,}["'"'"']' . \
    --exclude-dir=.git --exclude-dir=node_modules 2>/dev/null \
    | grep -vE 'password *[:=] *null|password *==|"(storcli|config)"' || true)
[ -z "$assigned" ] && pass "无硬编码口令 / 令牌赋值" || fail "疑似硬编码凭据：$assigned"

# --------------------------------------------------------------------------- #
section "7. 敏感信息：主机、网络与真机标识"
# --------------------------------------------------------------------------- #
generic=$(grep -rInE '192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|/home/[a-z][a-z0-9_-]*/|@(gmail|qq|163|outlook)\.com' . \
    --exclude-dir=.git 2>/dev/null || true)
[ -z "$generic" ] && pass "无内网 IP / 用户家目录 / 个人邮箱" || fail "疑似私有信息：$generic"

if [ "$CI_MODE" -eq 1 ]; then
    pass "CI 模式：跳过本机 deny 列表（不含真机序列号等私有串）"
elif [ -r "$DENY" ]; then
    deny_hits=0
    while IFS= read -r token; do
        [ -z "$token" ] && continue
        case "$token" in \#*) continue ;; esac
        found=$(grep -rIilF "$token" . --exclude-dir=.git 2>/dev/null || true)
        [ -n "$found" ] && { fail "命中 deny 串「$token」：$found"; deny_hits=$((deny_hits + 1)); }
    done <"$DENY"
    [ "$deny_hits" -eq 0 ] && pass "未命中本机 deny 列表（$(grep -cve '^#' -e '^$' "$DENY") 条真机标识）"
else
    warn "未找到 deny 列表 $DENY —— 真机序列号 / SAS 地址 / 主机名不会被检查"
fi

# --------------------------------------------------------------------------- #
section "8. 打包（fnpack）"
# --------------------------------------------------------------------------- #
if [ "$CI_MODE" -eq 1 ] || ! command -v fnpack >/dev/null 2>&1; then
    # 用布局检查替代：确保 fnpack 需要的入口都在
    layout_ok=1
    grep -q '^desktop_uidir *=' manifest || layout_ok=0
    grep -q '^desktop_applaunchname *=' manifest || layout_ok=0
    [ -f app/ui/config ] || layout_ok=0
    [ "$layout_ok" -eq 1 ] && pass "打包布局入口齐全（未实际打 fpk）" || fail "manifest / ui config 缺失打包字段"
else
    tmp=$(mktemp -d)
    cp -a . "$tmp/src" 2>/dev/null
    rm -rf "$tmp/src/.git"
    if ( cd "$tmp/src" && fnpack build -d . >/dev/null 2>&1 ); then
        fpk_ver=$(tar -xzOf "$tmp/src/megaraid-console.fpk" manifest 2>/dev/null | sed -n 's/^version[[:space:]]*=[[:space:]]*//p' | head -1)
        [ "$fpk_ver" = "$ver_manifest" ] && pass "fnpack 打包成功，包内版本 $fpk_ver" \
            || fail "包内版本 $fpk_ver 与 manifest $ver_manifest 不一致"
        fpk_listing=$(tar -tzf "$tmp/src/megaraid-console.fpk" 2>/dev/null || true)
        # 用 printf 喂给不带 -q 的 grep：grep -q 提前退出会让 tar 收到 SIGPIPE，
        # 在 pipefail 下整条管道被判为失败。
        if printf '%s\n' "$fpk_listing" | grep -x 'LICENSE' >/dev/null; then
            pass "包内含 LICENSE（GPL 要求）"
        else
            fail "包内缺少 LICENSE"
        fi
        fpk_verbose=$(tar -tvzf "$tmp/src/megaraid-console.fpk" 2>/dev/null || true)
        # tar -tv 的列是「权限 属主 大小 日期 时间 名字」，名字在第 6 列
        if printf '%s\n' "$fpk_verbose" | awk '{print $1, $6}' \
                | grep -F -- '-rwxr-xr-x cmd/main' >/dev/null; then
            pass "包内 cmd/main 可执行"
        else
            fail "包内 cmd/main 丢失可执行位"
        fi
    else
        fail "fnpack build 失败"
    fi
    rm -rf "$tmp"
fi

# --------------------------------------------------------------------------- #
printf '\n\033[1m结论:\033[0m '
if [ "$FAIL" -eq 0 ]; then
    printf '\033[32m通过\033[0m（%d 项警告）\n' "$WARN"
    exit 0
else
    printf '\033[31m%d 项失败\033[0m（%d 项警告）\n' "$FAIL" "$WARN"
    exit 1
fi
