#!/bin/bash

# =============================
# iptables 管理脚本 v1.0
# 功能描述：
# - 交互式管理 iptables 规则和服务
# - 支持多端口、多 IP / IP段 (CIDR)
# - 自动检测端口和 IP 格式
# - 新增规则前自动备份，操作后保存/恢复
# - 支持 systemd 服务操作（开机自启）
# - 默认工作目录 /app/iptables
# =============================

VERSION="1.0"

# -----------------------------
# 检查 root 权限
# -----------------------------
if [[ $EUID -ne 0 ]]; then
    echo "请使用 root 用户运行此脚本"
    exit 1
fi

# -----------------------------
# 检查依赖
# -----------------------------
for cmd in iptables iptables-save iptables-restore systemctl logger; do
    command -v $cmd >/dev/null 2>&1 || { echo "请先安装 $cmd"; exit 1; }
done

# -----------------------------
# 工作目录和文件
# -----------------------------
WORK_DIR="/app/iptables"
RULES_FILE="$WORK_DIR/rules.v4"
LOG_FILE="$WORK_DIR/iptables.log"
SERVICE_FILE="/etc/systemd/system/iptables-restore.service"
mkdir -p "$WORK_DIR"

# -----------------------------
# 日志函数
# -----------------------------
log() {
    local msg="$1"
    echo "$(date '+%F %T') [$(whoami)] $msg" | tee -a "$LOG_FILE"
}

# -----------------------------
# 初始化默认规则
# -----------------------------
init_rules() {
    iptables -F
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT ACCEPT

    iptables -A INPUT -i lo -j ACCEPT
    iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT -p tcp --dport 22 -j ACCEPT   # 默认允许 SSH
    log "已初始化默认规则"
}

# -----------------------------
# 工具函数：端口/IP检测
# -----------------------------
validate_port() {
    local port=$1
    if [[ "$port" =~ ^[0-9]{1,5}$ ]] && ((port>=1 && port<=65535)); then
        return 0
    elif [[ "$port" =~ ^[0-9]{1,5}:[0-9]{1,5}$ ]]; then
        IFS=':' read -r start end <<< "$port"
        if ((start>=1 && start<=65535 && end>=1 && end<=65535 && start<=end)); then
            return 0
        fi
    fi
    return 1
}

validate_ip() {
    local ip=$1
    if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]]; then
        IFS='/' read -r base cidr <<< "$ip"
        IFS='.' read -ra octets <<< "$base"
        for octet in "${octets[@]}"; do
            if ((octet<0 || octet>255)); then
                return 1
            fi
        done
        if [[ -n "$cidr" ]] && ((cidr<0 || cidr>32)); then
            return 1
        fi
        return 0
    fi
    return 1
}

# -----------------------------
# 保存/恢复规则
# -----------------------------
save_rules() {
    # 备份旧规则
    if [[ -f "$RULES_FILE" ]]; then
        cp "$RULES_FILE" "$RULES_FILE.bak.$(date '+%F_%H%M%S')"
    fi
    iptables-save > "$RULES_FILE"
    log "规则已保存到 $RULES_FILE"
}

restore_rules() {
    if [[ -f "$RULES_FILE" ]]; then
        iptables -F
        iptables-restore < "$RULES_FILE" || log "恢复规则失败"
        log "规则已从 $RULES_FILE 恢复"
    else
        log "规则文件不存在，初始化默认规则"
        init_rules
    fi
}

show_rules() {
    iptables -L -n --line-numbers
}

# -----------------------------
# 添加规则
# -----------------------------
add_rule() {
    read -p "请输入允许访问的端口(可逗号或范围，例如 22,80,443 或 1000:2000): " ports
    ports=$(echo "$ports" | tr -d ' ' | tr 'A-Z' 'a-z')
    read -p "是否限制特定 IP 或 IP 段? (y/n): " limit_ip
    limit_ip=$(echo "$limit_ip" | tr -d ' ' | tr 'A-Z' 'a-z')

    port_array=()
    for p in ${ports//,/ }; do
        if validate_port "$p"; then
            port_array+=("$p")
        else
            echo "端口格式错误: $p"
            return
        fi
    done

    if [[ "$limit_ip" == "y" ]]; then
        read -p "请输入允许访问的 IP 或 CIDR（可逗号分隔，例如 192.168.1.10,10.0.0.0/24）: " ips
        ips=$(echo "$ips" | tr -d ' ')
        ip_array=()
        for ip in ${ips//,/ }; do
            if validate_ip "$ip"; then
                ip_array+=("$ip")
            else
                echo "IP/CIDR 格式错误: $ip"
                return
            fi
        done

        for ip in "${ip_array[@]}"; do
            for port in "${port_array[@]}"; do
                if ! iptables -C INPUT -p tcp -s "$ip" --dport "$port" -j ACCEPT 2>/dev/null; then
                    iptables -A INPUT -p tcp -s "$ip" --dport "$port" -j ACCEPT
                    log "已允许 IP/IP段 $ip 访问端口 $port"
                    sleep 0.05
                else
                    log "规则已存在：$ip 端口 $port"
                fi
            done
        done
    else
        for port in "${port_array[@]}"; do
            if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
                iptables -A INPUT -p tcp --dport "$port" -j ACCEPT
                log "已允许所有 IP 访问端口 $port"
                sleep 0.05
            else
                log "规则已存在：所有 IP 端口 $port"
            fi
        done
    fi

    save_rules
    restore_rules
}

# -----------------------------
# 删除规则
# -----------------------------
delete_rule() {
    show_rules
    read -p "请输入要删除的规则编号: " num
    if [[ "$num" =~ ^[0-9]+$ ]]; then
        read -p "确认删除规则编号 $num ? (y/n): " confirm
        [[ "$confirm" != "y" ]] && return
        iptables -D INPUT "$num"
        log "已删除规则编号 $num"
        save_rules
        restore_rules
    else
        echo "请输入正确的编号"
    fi
}

modify_rule() {
    echo "提示：iptables 不支持直接修改规则，需要先删除再添加"
    delete_rule
    add_rule
}

# -----------------------------
# systemd 服务管理
# -----------------------------
create_service() {
    ip_restore_path=$(command -v iptables-restore)
    echo "[Unit]
Description=Restore iptables rules
After=network.target

[Service]
Type=oneshot
ExecStart=$ip_restore_path < $RULES_FILE
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target" > "$SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable iptables-restore.service
    log "已创建 systemd 服务，并设置开机自启"
}

start_service() { systemctl start iptables-restore.service && log "服务已启动"; }
stop_service() { systemctl stop iptables-restore.service && log "服务已停止"; }
restart_service() { systemctl restart iptables-restore.service && log "服务已重启"; }
status_service() { systemctl status iptables-restore.service --no-pager; }
enable_service() { systemctl enable iptables-restore.service && log "服务已设置开机自启"; }
disable_service() { systemctl disable iptables-restore.service && log "服务开机自启已取消"; }

# -----------------------------
# 菜单函数
# -----------------------------
iptables_rules_menu() {
    while true; do
        echo ""
        echo "=== iptables 规则管理 ==="
        echo "1) 新增规则"
        echo "2) 删除规则"
        echo "3) 修改规则"
        echo "4) 查看规则"
        echo "5) 保存规则"
        echo "6) 恢复规则"
        echo "0) 返回上级菜单"
        read -p "请选择 [0-6]: " choice
        case $choice in
            1) add_rule ;;
            2) delete_rule ;;
            3) modify_rule ;;
            4) show_rules ;;
            5) save_rules ;;
            6) restore_rules ;;
            0) break ;;
            *) echo "无效选项" ;;
        esac
    done
}

iptables_service_menu() {
    while true; do
        echo ""
        echo "=== iptables 服务管理 ==="
        echo "1) 启动服务"
        echo "2) 停止服务"
        echo "3) 重启服务"
        echo "4) 查看服务状态"
        echo "5) 设置开机自启"
        echo "6) 取消开机自启"
        echo "0) 返回上级菜单"
        read -p "请选择 [0-6]: " choice
        case $choice in
            1) start_service ;;
            2) stop_service ;;
            3) restart_service ;;
            4) status_service ;;
            5) enable_service ;;
            6) disable_service ;;
            0) break ;;
            *) echo "无效选项" ;;
        esac
    done
}

# -----------------------------
# 主菜单
# -----------------------------
create_service
restore_rules

while true; do
    echo ""
    echo "=== iptables 管理脚本 v$VERSION ==="
    echo "1) iptables 规则管理"
    echo "2) iptables 服务管理"
    echo "0) 退出"
    read -p "请选择 [0-2]: " main_choice
    case $main_choice in
        1) iptables_rules_menu ;;
        2) iptables_service_menu ;;
        0) exit 0 ;;
        *) echo "无效选项" ;;
    esac
done
