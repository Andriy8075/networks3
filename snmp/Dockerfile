FROM alpine:3.22

RUN apk add --no-cache net-snmp net-snmp-tools

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["snmpd"]
