# Build a static listener, then run it without a shell or root privileges.
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/webhook-listener .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/webhook-listener /usr/local/bin/webhook-listener
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/webhook-listener"]
