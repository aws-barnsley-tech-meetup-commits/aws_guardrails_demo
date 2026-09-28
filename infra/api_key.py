"""The shared API key, generated during the deploy.

``POST /c1/attack`` is guarded by one shared secret. It used to be a manual step:
run ``openssl rand -hex 32``, put it in Parameter Store yourself, paste the ARN
into ``.env``. That is three chances to get it wrong before the first deploy, so
the stack does it instead.

The key is generated inside a custom resource rather than at synth time, because
anything resolved at synth ends up in the CloudFormation template and in
``cdk.out`` on disk. CloudFormation has no native ``SecureString`` support, which
is the other reason this is a custom resource and not a plain
``AWS::SSM::Parameter``.

The Lambda still reads the key from Parameter Store at request time and caches it
for five minutes, exactly as before, so rotating by hand still works:

    aws ssm put-parameter --name /guardrails-demo/api-key \\
        --type SecureString --value "$(openssl rand -hex 32)" --overwrite

A later ``cdk deploy`` will keep whatever value is stored rather than clobbering
it.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import custom_resources as cr
from constructs import Construct

HANDLER_DIR = Path(__file__).resolve().parent / "api_key_handler"

#: Where the key lives. One deploy per account and Region is the assumption; a
#: second stack in the same Region should pass a different name.
DEFAULT_PARAMETER_NAME = "/guardrails-demo/api-key"


class ApiKey(Construct):
    """Generates the shared API key and keeps it in Parameter Store.

    Exposes the value as ``api_key.value`` so the stack can output it. That value
    is a CloudFormation token resolved at deploy time, not a synth-time string.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        parameter_name: str = DEFAULT_PARAMETER_NAME,
    ) -> None:
        super().__init__(scope, construct_id)

        stack = Stack.of(self)
        self.parameter_name = parameter_name
        self.parameter_arn = stack.format_arn(
            service="ssm",
            resource="parameter",
            # format_arn would otherwise insert a separator before the leading
            # slash and produce ".../parameter//guardrails-demo/api-key".
            resource_name=parameter_name.lstrip("/"),
        )

        on_event = lambda_.Function(
            self,
            "Generator",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_asset(str(HANDLER_DIR), exclude=["__pycache__"]),
            timeout=Duration.seconds(30),
            description="Generates and stores the guardrails demo API key.",
            log_group=logs.LogGroup(
                self,
                "GeneratorLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )

        # Scoped to the one parameter this construct owns. GetParameter is needed
        # so an update can read the current value back instead of rotating it.
        on_event.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageApiKeyParameter",
                actions=[
                    "ssm:PutParameter",
                    "ssm:GetParameter",
                    "ssm:DeleteParameter",
                ],
                resources=[self.parameter_arn],
            )
        )

        provider = cr.Provider(
            self,
            "Provider",
            on_event_handler=on_event,
            log_group=logs.LogGroup(
                self,
                "ProviderLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )

        self.resource = CustomResource(
            self,
            "Resource",
            service_token=provider.service_token,
            resource_type="Custom::DemoApiKey",
            properties={"ParameterName": parameter_name},
        )

    @property
    def value(self) -> str:
        """The key itself, as a deploy-time token."""
        return self.resource.get_att_string("ApiKey")

    def grant_read(self, grantee: iam.IGrantable) -> None:
        """Let something read the key from Parameter Store at runtime."""
        grantee.grant_principal.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ReadApiKeyParameter",
                actions=["ssm:GetParameter"],
                resources=[self.parameter_arn],
            )
        )
