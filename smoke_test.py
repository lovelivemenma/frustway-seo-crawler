import crawler


def expect_block(url):
    try:
        crawler.validate_public_url(url)
    except Exception:
        return
    raise AssertionError(f"Expected block: {url}")


def main():
    expect_block("http://127.0.0.1/")
    expect_block("http://10.0.0.1/")
    expect_block("http://192.168.1.1/")
    expect_block("http://localhost/")
    expect_block("https://8.8.8.8:8080/")

    assert crawler.validate_public_url(
        "https://8.8.8.8/"
    )

    print("Security smoke test: OK")


if __name__ == "__main__":
    main()
