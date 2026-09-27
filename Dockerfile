FROM alpine:3.20
RUN apk add --no-cache openvpn iproute2 bash
# Keep the container alive; openvpn is launched separately via `docker exec -d`
# so we can poll for tun0 coming up before handing control back to the user.
ENTRYPOINT ["tail", "-f", "/dev/null"]
