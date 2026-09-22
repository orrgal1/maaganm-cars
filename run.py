"""Run the maaganm-cars API with its configured local binding."""


def main() -> None:
    import uvicorn

    import config

    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
