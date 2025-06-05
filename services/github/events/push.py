def process_push_event(payload: dict, conn):
    # Extraer datos del commit
    head_commit = payload.get("head_commit", {})
    sha = head_commit.get("id")
    message = head_commit.get("message")
    timestamp = head_commit.get("timestamp")
    repo = payload.get("repository", {}).get("full_name")

    # Guardar en tabla de feedback, o imprimir, etc.
    print("💾 SHA:", sha)
    print("📘 Message:", message)
    print("📅 Fecha:", timestamp)
    print("📦 Repo:", repo)
    # Aquí podrías hacer cosas como analizar el código, etc.