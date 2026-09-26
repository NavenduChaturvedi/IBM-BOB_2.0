from blastradius.artifacts import scan_artifacts
from blastradius.diff_parser import parse_diff

DJANGO_0002 = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial"), ("auth", "0012_x")]
    operations = [
        migrations.AddField(model_name="order", name="note", field=models.TextField(null=True)),
        migrations.RunPython(backfill),
    ]
"""

MANIFEST = """\
apiVersion: v1
kind: Service
metadata:
  name: web
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: worker
spec:
  template:
    spec:
      containers:
        - name: worker
          env:
            - name: QUEUE_URL
              value: redis://
"""

COMPOSE = """\
services:
  web:
    environment:
      - SECRET_KEY=x
  db:
    environment:
      POSTGRES_PASSWORD: x
"""


def test_scan(tmp_repo):
    tmp_repo.commit("init", {
        "shop/migrations/__init__.py": "",
        "shop/migrations/0001_initial.py": "from django.db import migrations\n",
        "deploy/worker.yaml": MANIFEST,
        "docker-compose.yml": COMPOSE,
        "infra/{{broken}}.yaml": "key: {{ .Values.x }}\n",  # helm template must not crash the scan
        "Dockerfile": "FROM python:3.11\nENV PORT=8000\n",
    })
    tmp_repo.branch("feature")
    tmp_repo.commit("m", {"shop/migrations/0002_note.py": DJANGO_0002})
    facts = scan_artifacts(tmp_repo.path, parse_diff(tmp_repo.path, "main", "feature"))

    mig = next(m for m in facts.migrations if m.revision == "0002_note")
    assert (mig.tool, mig.app, mig.down_revision, mig.in_diff) == ("django", "shop", "0001_initial", True)
    assert not mig.has_downgrade  # RunPython without reverse_code
    assert [o.op for o in mig.upgrade_ops] == ["AddField", "RunPython"]
    assert not mig.upgrade_ops[0].risky and mig.upgrade_ops[1].risky

    [worker] = facts.k8s_deployments
    assert (worker.kind, worker.name, worker.namespace, worker.env) == ("StatefulSet", "worker", "default", ["QUEUE_URL"])
    assert sorted(facts.compose_services) == ["db", "web"]
    assert {"QUEUE_URL", "SECRET_KEY", "POSTGRES_PASSWORD", "PORT"} <= set(facts.declared_env)
    assert facts.dockerfiles == ["Dockerfile"]
