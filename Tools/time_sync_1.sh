#!/bin/bash

# ===================================================
# Debian / Ubuntu 时间同步交互式脚本
# - 设置时区（默认 Asia/Shanghai，可交互选择）
# - 使用 systemd-timesyncd 同步时间
# - 支持选择/修改/查看公共 NTP
# ===================================================

set -e

# 默认时区
DEFAULT_TIMEZONE="Asia/Shanghai"

# 公共 NTP 服务器列表
declare -A NTP_SERVERS=(
    [1]="谷歌: time1.google.com time2.google.com time3.google.com time4.google.com"
    [2]="微软: time.windows.com"
    [3]="Cloudflare: time.cloudflare.com"
    [4]="苹果: time1.apple.com time2.apple.com time3.apple.com time4.apple.com time5.apple.com time6.apple.com time7.apple.com"
    [5]="国家授时中心: ntp.ntsc.ac.cn"
    [6]="阿里云: ntp.aliyun.com ntp1.aliyun.com ntp2.aliyun.com ntp3.aliyun.com ntp4.aliyun.com ntp5.aliyun.com ntp6.aliyun.com ntp7.aliyun.com"
    [7]="腾讯云: ntp.tencent.com ntp1.tencent.com ntp2.tencent.com ntp3.tencent.com ntp4.tencent.com ntp5.tencent.com"
)
FALLBACK="pool.ntp.org 0.pool.ntp.org 1.pool.ntp.org 2.pool.ntp.org 3.pool.ntp.org"

# 安装 systemd-timesyncd
# echo "===> 安装 systemd-timesyncd（静默安装）"
export DEBIAN_FRONTEND=noninteractive
apt update -qq > /dev/null 2>&1
apt install -y -qq systemd-timesyncd > /dev/null 2>&1

while true; do
    echo ""
    echo "请选择操作："
    echo "1) 设置时区（默认上海）"
    echo "2) 设置 NTP 服务器"
    read -p "输入数字 (1-2): " main_choice

    case "$main_choice" in
        1)
            echo ""
            echo "===> 可用时区列表（部分示例）"
            # 列出所有时区
            TIMEZONES=($(timedatectl list-timezones))
            for i in "${!TIMEZONES[@]}"; do
                printf "%d) %s\n" "$((i+1))" "${TIMEZONES[$i]}"
            done
            read -p "请输入数字选择时区（默认 $DEFAULT_TIMEZONE）: " tz_choice
            if [[ -z "$tz_choice" ]]; then
                SELECTED_TZ="$DEFAULT_TIMEZONE"
            else
                SELECTED_TZ="${TIMEZONES[$((tz_choice-1))]}"
            fi
            echo "===> 设置时区为 $SELECTED_TZ"
            timedatectl set-timezone "$SELECTED_TZ"
            echo "✅ 时区设置完成: $(timedatectl | grep "Time zone")"
            ;;
        2)
            while true; do
                echo ""
                echo "请选择操作："
                echo "1) 选择 NTP 服务器"
                echo "2) 修改 NTP 服务器"
                echo "3) 查看 NTP 服务器"
                read -p "输入数字 (1-3): " action

                case "$action" in
                    1|2)
                        if [[ "$action" == "2" && ! -f /etc/systemd/timesyncd.conf ]]; then
                            echo "⚠️ 未检测到配置，请先选择操作 1 设置 NTP"
                            continue
                        fi
                        if [[ "$action" == "2" ]]; then
                            CURRENT_NTP=$(grep -E '^NTP=' /etc/systemd/timesyncd.conf | cut -d= -f2-)
                            echo ""
                            echo "当前已配置 NTP: $CURRENT_NTP"
                            echo "请选择新的公共 NTP 服务器："
                        else
                            echo ""
                            echo "请选择公共 NTP 服务器："
                        fi

                        for i in {1..7}; do
                            printf "%d) %s\n" "$i" "${NTP_SERVERS[$i]}"
                        done
                        read -p "输入数字选择 NTP: " choice
                        NTP_SELECTION="${NTP_SERVERS[$choice]}"
                        if [ -z "$NTP_SELECTION" ]; then
                            echo "❌ 无效选择，请重新输入"
                            continue
                        fi
                        ;;
                    3)
                        if [ ! -f /etc/systemd/timesyncd.conf ]; then
                            echo "⚠️ 未检测到配置的 NTP 服务器"
                        else
                            CURRENT_NTP=$(grep -E '^NTP=' /etc/systemd/timesyncd.conf | cut -d= -f2-)
                            CURRENT_FALLBACK=$(grep -E '^FallbackNTP=' /etc/systemd/timesyncd.conf | cut -d= -f2-)
                            echo ""
                            echo "当前已配置 NTP: $CURRENT_NTP"
                            echo "Fallback NTP: $CURRENT_FALLBACK"
                        fi
                        continue
                        ;;
                    *)
                        echo "❌ 无效输入，请重试"
                        continue
                        ;;
                esac

                NTP_DOMAINS=$(echo "$NTP_SELECTION" | cut -d: -f2)

                # 检测可用性（只检查第一个可用服务器）
                AVAILABLE=""
                for server in $NTP_DOMAINS; do
                    if ping -c 2 -W 2 $server >/dev/null 2>&1; then
                        AVAILABLE="$server"
                        break
                    fi
                done

                if [ -z "$AVAILABLE" ]; then
                    echo "⚠️ 所选 NTP 服务器不可用，请重新选择"
                    continue
                else
                    echo "✅ 使用 NTP 服务器: $NTP_DOMAINS"
                    NTP_SERVER="$NTP_DOMAINS"
                    break
                fi
            done

            # 写入 timesyncd 配置
            cat > /etc/systemd/timesyncd.conf <<EOF
[Time]
NTP=$NTP_SERVER
FallbackNTP=$FALLBACK
EOF

            # echo "===> 重启并启用 systemd-timesyncd"
            systemctl restart systemd-timesyncd
            systemctl enable systemd-timesyncd
            timedatectl set-ntp true
            echo "✅ NTP 配置完成"
            ;;
        *)
            echo "❌ 无效输入，请重新选择"
            continue
            ;;
    esac

    echo ""
    read -p "是否继续操作？(y/n): " cont
    if [[ "$cont" != [Yy] ]]; then
        break
    fi
done

echo "===> 当前时间状态："
timedatectl status

echo "===> 同步详情："
timedatectl timesync-status || true

echo "=================================="
echo "✅ 配置完成"
echo "✅ 时区：$(timedatectl | grep 'Time zone')"
echo "✅ NTP：$NTP_SERVER"
echo "=================================="
