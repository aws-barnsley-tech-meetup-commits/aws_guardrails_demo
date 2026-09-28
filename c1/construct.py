"""Challenge C1, "Break the Bot".

Creates the QuickCart bot on AgentCore Runtime, the Lambda that invokes it and
judges the reply, and the C1 routes on the shared HTTP API.

The bot answers with no guardrail by default. A request can ask for one with
``x-guardrail: on``, and can pick which model answers with ``x-model-tier``, so
both model tiers and the guardrail have to be reachable from the runtime role.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.api_key import ApiKey
from infra.bundling import build_agent_bundle, build_api_bundle
from infra.guardrail import DemoGuardrail
from infra.model_arns import model_invoke_resources

#: AgentCore terminates a runtime session after this much idle time.
IDLE_SESSION_TIMEOUT = Duration.seconds(900)

#: The file AgentCore runs, relative to the root of the code zip.
#:
#: Just the filename, not ["python", "main.py"]. AgentCore validates each
#: entrypoint element as a path and rejects the interpreter form with
#: "Invalid entrypoint value ... contains spaces ... or has multiple dots".
#: It supplies the interpreter itself. The only supported two-element form is
#: ["opentelemetry-instrument", "main.py"], which additionally requires the
#: aws-opentelemetry-distro package inside the bundle.
AGENT_ENTRYPOINT = "main.py"

#: The Lambda waits on the agent then on the judge, so it sits just under the
#: 30 second HTTP API integration ceiling.
LAMBDA_TIMEOUT = Duration.seconds(29)


class C1Construct(Construct):
    """The AgentCore Runtime, the API Lambda and the C1 routes."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        http_api: apigwv2.HttpApi,
        bot_model_id: str,
        bot_model_id_premium: str,
        judge_model_id: str,
        guardrail: DemoGuardrail,
        api_key: ApiKey,
    ) -> None:
        super().__init__(scope, construct_id)

        stack = Stack.of(self)
        self.bot_model_id = bot_model_id
        self.bot_model_id_premium = bot_model_id_premium
        self.judge_model_id = judge_model_id

        # Both are created by this stack, so these are deploy-time tokens rather
        # than values someone pasted into .env.
        self.guardrail = guardrail
        self.api_key = api_key
        guardrail_id = guardrail.guardrail_id
        guardrail_version = guardrail.version
        # Read off the resource instead of assembled from the id, so a change to
        # how Bedrock formats guardrail ARNs cannot silently break the grant.
        self.guardrail_arn = guardrail.guardrail_arn

        # --- The bot -------------------------------------------------------
        # Direct code deployment: a Python zip, no Docker, no ECR.
        agent_bundle = build_agent_bundle()
        entrypoint_file = agent_bundle / AGENT_ENTRYPOINT
        if not entrypoint_file.is_file():
            # Otherwise this only surfaces as a CREATE_FAILED minutes later.
            raise FileNotFoundError(
                f"agent bundle is missing its entrypoint: {entrypoint_file}"
            )

        self.runtime = agentcore.Runtime(
            self,
            "BotRuntime",
            runtime_name="quickcart_c1_bot",
            description="QuickCart support bot for C1. No guardrail, single turn.",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_code_asset(
                path=str(agent_bundle),
                runtime=agentcore.AgentCoreRuntime.PYTHON_3_13,
                entrypoint=[AGENT_ENTRYPOINT],
                # Local bytecode targets the wrong platform. The bundler strips
                # it, this stops anything created later from sneaking in.
                exclude=["__pycache__"],
            ),
            environment_variables={
                # Both tiers, and the guardrail the request may ask for. Never
                # hardcoded in the agent: it reads all four from here.
                "BOT_MODEL_ID": bot_model_id,
                "BOT_MODEL_ID_PREMIUM": bot_model_id_premium,
                "GUARDRAIL_ID": guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
            },
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=IDLE_SESSION_TIMEOUT,
            ),
        )

        # Either tier can be asked for per request, so grant both. Dedupe
        # because the two ids may share a foundation model.
        bot_model_resources = sorted(
            set(
                model_invoke_resources(
                    bot_model_id, region=stack.region, account=stack.account
                )
            )
            | set(
                model_invoke_resources(
                    bot_model_id_premium, region=stack.region, account=stack.account
                )
            )
        )
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeBotModels",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=bot_model_resources,
            )
        )

        # Converse with guardrailConfig needs ApplyGuardrail on the guardrail
        # itself, not just InvokeModel on the model.
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="ApplyBotGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.guardrail_arn],
            )
        )

        # --- The API -------------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "ApiFunctionLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "ApiFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                str(build_api_bundle()), exclude=["__pycache__"]
            ),
            timeout=LAMBDA_TIMEOUT,
            memory_size=512,
            log_group=log_group,
            environment={
                "AGENT_RUNTIME_ARN": self.runtime.agent_runtime_arn,
                "JUDGE_MODEL_ID": judge_model_id,
                # /c1/guardrail-check calls ApplyGuardrail directly, and needs
                # the same guardrail the runtime uses.
                "GUARDRAIL_ID": guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
                # mode=full asks the bot for a reply before grading it, so the
                # Lambda needs to know which tier to request.
                "BOT_MODEL_ID": bot_model_id,
                "BOT_MODEL_ID_PREMIUM": bot_model_id_premium,
                # The ARN, not the value. The Lambda reads the secret at runtime
                # so it never lands in the CloudFormation template.
                "API_KEY_PARAMETER_ARN": api_key.parameter_arn,
            },
        )

        # --- Authorization -------------------------------------------------
        api_key.grant_read(self.function)

        # The parameter ARN is derived from a name rather than read off the custom
        # resource, so nothing in the template forces an ordering. Say it out
        # loud: the function must not be reachable before the key exists, or the
        # first request answers 503.
        self.function.node.add_dependency(api_key)

        # bedrock-agentcore:InvokeAgentRuntime on the runtime and, via the
        # trailing wildcard the grant adds, on its endpoints.
        self.runtime.grant_invoke_runtime(self.function)

        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeJudgeModel",
                actions=["bedrock:InvokeModel"],
                resources=model_invoke_resources(
                    judge_model_id, region=stack.region, account=stack.account
                ),
            )
        )

        # /c1/guardrail-check runs ApplyGuardrail itself, for both stages.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ApplyGuardrailForCheck",
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.guardrail_arn],
            )
        )

        # --- Routes --------------------------------------------------------
        integration = integrations.HttpLambdaIntegration("C1Integration", self.function)
        http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )
        http_api.add_routes(
            path="/c1/attack",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )
        http_api.add_routes(
            path="/c1/prompts",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )
        http_api.add_routes(
            path="/c1/guardrail-check",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )

    @property
    def agent_runtime_arn(self) -> str:
        return self.runtime.agent_runtime_arn
