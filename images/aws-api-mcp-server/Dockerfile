# renovate: datasource=docker depName=public.ecr.aws/awslabs-mcp/awslabs/aws-api-mcp-server
ARG AWS_API_MCP_VERSION=1.5.0
FROM public.ecr.aws/awslabs-mcp/awslabs/aws-api-mcp-server:${AWS_API_MCP_VERSION}
# sitecustomize.py is auto-imported at interpreter startup (before server:main),
# where it monkeypatches the blocking AWS call off the event loop. See README.
COPY sitecustomize.py /opt/patch/sitecustomize.py
ENV PYTHONPATH=/opt/patch
# Python tolerates a failed automatic sitecustomize import, so explicitly import
# it during the image build. Any upstream drift therefore rejects the artifact
# instead of publishing a server that would run without the availability patch.
RUN PYTHONPATH=/opt/patch python -c 'import sitecustomize'
