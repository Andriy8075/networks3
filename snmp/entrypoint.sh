#!/bin/sh
set -eu

community="${SNMP_COMMUNITY:?SNMP_COMMUNITY is required}"

cat > /etc/snmp/snmpd.conf << EOF
rocommunity ${community} 172.31.0.0/24
rocommunity ${community} 127.0.0.1
sysName net-device
sysLocation Lab3 monitoring network
sysContact lab3
dontLogTCPWrappersConnects yes
EOF

if [ "${1:-}" = "snmpd" ]; then
    exec snmpd -f -Lo -c /etc/snmp/snmpd.conf
fi

exec "$@"
