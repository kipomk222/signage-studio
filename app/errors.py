"""画面にそのまま表示できるエラー。"""


class UserError(RuntimeError):
    """「何が起きたか」と「どうすればよいか」を1文ずつ持つエラー。

    画面ではこの2文だけを表示する。専門用語は入れない。
    """

    def __init__(self, what: str, how: str) -> None:
        super().__init__(f"{what}{how}")
        self.what = what
        self.how = how
