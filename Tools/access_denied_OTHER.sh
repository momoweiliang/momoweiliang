#!/bin/bash

# ----  屏蔽海外地区IP访问  ----

# 出错即退出（防止错误继续执行）
set -e

echo "===> 安装依赖（nftables + wget）"
apt update -y
apt install -y nftables wget

echo "===> 启用 nftables 开机启动"
systemctl enable nftables
systemctl start nftables

# 工作目录
WORKDIR="/app/nft-cn-block"

echo "===> 创建工作目录: $WORKDIR"
mkdir -p $WORKDIR
cd $WORKDIR

echo "===> 下载 APNIC IP 数据"
wget -q https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest -O apnic.txt

echo "===> 生成中国 IPv4 列表"
# 说明：
# $4 = IP 起始地址
# $5 = IP 数量 → 转换成 CIDR
grep '|CN|ipv4|' apnic.txt | awk -F'|' '{printf("%s/%d\n",$4,32-log($5)/log(2))}' > cn_ipv4.txt

echo "===> 生成中国 IPv6 列表"
# IPv6 直接就是前缀长度
grep '|CN|ipv6|' apnic.txt | awk -F'|' '{print $4 "/" $5}' > cn_ipv6.txt

echo "===> 写入 nftables 配置"

# 注意：这里仍然使用 /etc/nftables.conf（系统标准位置，不能乱改）
cat > /etc/nftables.conf << 'EOF'
#!/usr/sbin/nft -f

# 清空现有规则（避免冲突）
flush ruleset

table inet filter {

    # 中国 IPv4 地址集合
    set cn_ipv4 {
        type ipv4_addr
        flags interval
        auto-merge
    }

    # 中国 IPv6 地址集合
    set cn_ipv6 {
        type ipv6_addr
        flags interval
        auto-merge
    }

    chain input {
        type filter hook input priority 0;

        # 1️⃣ 允许本地回环
        iif lo accept

        # 2️⃣ 允许已建立连接
        ct state established,related accept

        # 3️⃣ 白名单（最高优先级）
        ip saddr {1.1.1.1, 1.1.1.2} accept

        # 4️⃣ 放行国内、封禁海外
        # ✅ 放行中国 IP
        ip saddr @cn_ipv4 accept
        ip6 saddr @cn_ipv6 accept

        # ❌ 不在集合的全部 drop，即封禁海外IP
        ip saddr != @cn_ipv4 drop
        ip6 saddr != @cn_ipv6 drop

        # 5️⃣ 允许指定端口（非 CN 可访问）
        tcp dport {21, 22} accept

        # 6️⃣ ICMP（可选）
        ip protocol icmp accept
        ip6 nexthdr icmpv6 accept

        # 7️⃣ 默认拒绝（核心）
        drop
    }
}
EOF

echo "===> 应用 nftables 规则"
nft -f /etc/nftables.conf

echo "===> 导入 IPv4 / IPv6 地址到 nftables"
for ipfile in cn_ipv4.txt cn_ipv6.txt; do
    while read ip; do
        setname=$(basename $ipfile .txt)
        nft add element inet filter $setname { $ip }
    done < $ipfile
done

echo "===> 创建自动更新脚本"

# --- 自动更新脚本 ---
cat > $WORKDIR/update.sh << 'EOF'
#!/bin/bash

# 自动更新 CN IP 列表脚本

set -e

cd /app/nft-cn-block

echo "===> 更新 APNIC 数据"
wget -q https://ftp.apnic.net/apnic/stats/apnic/delegated-apnic-latest -O apnic.txt

echo "===> 重新生成 IPv4 / IPv6 列表"
grep '|CN|ipv4|' apnic.txt | awk -F'|' '{printf("%s/%d\n",$4,32-log($5)/log(2))}' > cn_ipv4.txt
grep '|CN|ipv6|' apnic.txt | awk -F'|' '{print $4 "/" $5}' > cn_ipv6.txt

echo "===> 清空旧 IP 集合"
nft flush set inet filter cn_ipv4
nft flush set inet filter cn_ipv6

echo "===> 重新导入 IPv4 / IPv6 地址到 nftables，时间较长，请耐心等待！"
for ipfile in cn_ipv4.txt cn_ipv6.txt; do
    while read ip; do
        setname=$(basename $ipfile .txt)
        nft add element inet filter $setname { $ip }
    done < $ipfile
done

echo "===> 更新完成: $(date)"
EOF

chmod +x $WORKDIR/update.sh

echo "===> 添加定时任务（每天凌晨 3 点自动更新）"

# 临时文件
TMP_CRON=$(mktemp)

# 1️⃣ 读取现有 crontab，如果没有也不报错
crontab -l 2>/dev/null > $TMP_CRON || true

# 2️⃣ 检查是否已存在相同任务
if ! grep -Fq "/app/nft-cn-block/update.sh" $TMP_CRON; then
    echo "0 3 * * * /app/nft-cn-block/update.sh" >> $TMP_CRON
fi

# 3️⃣ 安装新的 crontab
crontab $TMP_CRON

# 4️⃣ 删除临时文件
rm -f $TMP_CRON

echo "===> 定时任务添加完成 ✅"

# 自动提取允许的端口并提示
ALLOWED_PORTS=$(grep 'tcp dport' /etc/nftables.conf | grep -o '{.*}' | tr -d '{} ')

echo "===> 部署完成 ✅"
echo "-----------------------------------"
echo "✔ 已屏蔽 海外 IPv4 + IPv6"
echo "✔ 数据目录: /app/nft-cn-block"
echo "✔ 自动每日更新"
echo "✔ 已允许访问的端口: $ALLOWED_PORTS"
echo "-----------------------------------"
