import requests

def call_llm(prompt: str, api_key: str) -> str:
    """
    Llama al modelo Gemini con el prompt dado y la API key especificada.

    Args:
        prompt (str): Texto de entrada para el modelo.
        api_key (str): API key de Google Gemini.

    Returns:
        str: Texto generado por Gemini o mensaje de error.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    try:
        response = requests.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print("❌ Error calling Gemini:", e)
        return "Error generating content."
