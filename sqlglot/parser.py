import logging

from sqlglot.errors import ErrorLevel, ParseError
from sqlglot.helper import list_get
from sqlglot.tokens import Token, TokenType
import sqlglot.expressions as exp


class Parser:
    """
    Parser consumes a list of tokens produced by the :class:`~sqlglot.tokens.Tokenizer`
    and produces a parsed syntax tree.

    Args
        functions (dict): the dictionary of additional functions in which the key
            represents a function's SQL name and the value is a function which constructs
            the function instance from a list of arguments.
        error_level (ErrorLevel): the desired error level. Default: ErrorLevel.RAISE.
        error_message_context (int): determines the amount of context to capture from
            a query string when displaying the error message (in number of characters).
            Default: 50.
    """

    def _parse_decimal(args):
        size = len(args)
        precision = args[0] if size > 0 else None
        scale = args[1] if size > 1 else None
        return exp.Decimal(precision=precision, scale=scale)

    FUNCTIONS = {
        **{name: f.from_arg_list for f in exp.ALL_FUNCTIONS for name in f.sql_names()},
        "DECIMAL": _parse_decimal,
        "NUMERIC": _parse_decimal,
    }

    TYPE_TOKENS = {
        TokenType.BOOLEAN,
        TokenType.TINYINT,
        TokenType.SMALLINT,
        TokenType.INT,
        TokenType.BIGINT,
        TokenType.FLOAT,
        TokenType.DOUBLE,
        TokenType.CHAR,
        TokenType.VARCHAR,
        TokenType.TEXT,
        TokenType.BINARY,
        TokenType.JSON,
        TokenType.TIMESTAMP,
        TokenType.TIMESTAMPTZ,
        TokenType.DATE,
        TokenType.ARRAY,
        TokenType.DECIMAL,
        TokenType.MAP,
    }

    # Tokens that can also be functions
    AMBIGUOUS_TOKEN_TYPES = {
        TokenType.ARRAY,
        TokenType.DATE,
        TokenType.MAP,
    }

    ID_VAR_TOKENS = {
        TokenType.IDENTIFIER,
        TokenType.VAR,
        TokenType.ALL,
        TokenType.ASC,
        TokenType.COLLATE,
        TokenType.COUNT,
        TokenType.DEFAULT,
        TokenType.DESC,
        TokenType.ENGINE,
        TokenType.FOLLOWING,
        TokenType.FORMAT,
        TokenType.IF,
        TokenType.INTERVAL,
        TokenType.ORDINALITY,
        TokenType.OVER,
        TokenType.PRECEDING,
        TokenType.RANGE,
        TokenType.ROWS,
        TokenType.SCHEMA_COMMENT,
        TokenType.UNBOUNDED,
        *TYPE_TOKENS,
    }

    PRIMARY_TOKENS = {
        TokenType.STRING,
        TokenType.NUMBER,
        TokenType.STAR,
        TokenType.NULL,
    }

    COLUMN_TOKENS = {
        *ID_VAR_TOKENS,
        TokenType.STAR,
    } - {TokenType.ARRAY}

    NON_COLUMN_TOKENS = {
        TokenType.COMMA,
        TokenType.R_PAREN,
        TokenType.WHEN,
    }

    CONJUNCTION = {
        TokenType.AND: exp.And,
        TokenType.OR: exp.Or,
    }

    EQUALITY = {
        TokenType.EQ: exp.EQ,
        TokenType.NEQ: exp.NEQ,
        TokenType.IS: exp.Is,
    }

    COMPARISON = {
        TokenType.GT: exp.GT,
        TokenType.GTE: exp.GTE,
        TokenType.LT: exp.LT,
        TokenType.LTE: exp.LTE,
    }

    BITWISE = {
        TokenType.LSHIFT: exp.BitwiseLeftShift,
        TokenType.RSHIFT: exp.BitwiseRightShift,
        TokenType.AMP: exp.BitwiseAnd,
        TokenType.CARET: exp.BitwiseXor,
        TokenType.PIPE: exp.BitwiseOr,
        TokenType.DPIPE: exp.DPipe,
    }

    TERM = {
        TokenType.DASH: exp.Minus,
        TokenType.PLUS: exp.Plus,
        TokenType.MOD: exp.Mod,
    }

    FACTOR = {
        TokenType.DIV: exp.IntDiv,
        TokenType.SLASH: exp.Div,
        TokenType.STAR: exp.Mul,
    }

    TOKEN_TO_EXPRESSION = {
        TokenType.STAR: lambda t: exp.Star(),
        TokenType.NULL: lambda t: exp.Null(),
        TokenType.STRING: lambda t: exp.Literal.string(t.text),
        TokenType.NUMBER: lambda t: exp.Literal.number(t.text),
        TokenType.IDENTIFIER: lambda t: exp.Identifier(this=t.text, quoted=True),
        TokenType.VAR: lambda t: exp.Identifier(this=t.text, quoted=False),
        **{
            t: lambda t: exp.DataType(this=exp.DataType.Type[t.token_type.value])
            for t in TYPE_TOKENS
        },
    }

    def __init__(self, functions=None, error_level=None, error_message_context=None):
        self.functions = {**self.FUNCTIONS, **(functions or {})}
        self.error_level = error_level or ErrorLevel.RAISE
        self.error_message_context = error_message_context or 50
        self.reset()

    def reset(self):
        self.code = ""
        self.error = None
        self._tokens = []
        self._chunks = [[]]
        self._index = 0
        self._curr = None
        self._next = None
        self._prev = None

    def parse(self, raw_tokens, code=None):
        """
        Parses the given list of tokens and returns a list of syntax trees, one tree
        per parsed SQL statement.

        Args
            raw_tokens (list): the list of tokens (:class:`~sqlglot.tokens.Token`).
            code (str): the original SQL string. Used to produce helpful debug messages.

        Returns
            the list of syntax trees (:class:`~sqlglot.expressions.Expression`).
        """
        self.reset()
        self.code = code or ""
        total = len(raw_tokens)

        for i, token in enumerate(raw_tokens):
            if token.token_type == TokenType.SEMICOLON:
                if i < total - 1:
                    self._chunks.append([])
            else:
                self._chunks[-1].append(token)

        expressions = []

        for tokens in self._chunks:
            self._index = -1
            self._tokens = tokens
            self._advance()
            expressions.append(self._ensure_non_token(self._parse_statement()))

            if self._index < len(self._tokens):
                self.raise_error("Invalid expression / Unexpected token")

        for expression in expressions:
            if not isinstance(expression, exp.Expression):
                continue
            for node, parent, key in expression.walk():
                if hasattr(node, "parent") and parent:
                    node.parent = parent
                    node.arg_key = key

        return expressions

    def raise_error(self, message, token=None):
        token = token or self._curr or self._prev or Token.string("")
        start = self._find_token(token, self.code)
        end = start + len(token.text)
        start_context = self.code[max(start - self.error_message_context, 0) : start]
        highlight = self.code[start:end]
        end_context = self.code[end : end + self.error_message_context]
        self.error = ParseError(
            f"{message}. Line {token.line}, Col: {token.col}.\n"
            f"{start_context}\033[4m{highlight}\033[0m{end_context}"
        )
        if self.error_level == ErrorLevel.RAISE:
            raise self.error
        if self.error_level == ErrorLevel.WARN:
            logging.error(self.error)

    def expression(self, exp_class, **kwargs):
        kwargs = {
            k: self._ensure_non_token(arg)
            if not isinstance(arg, list)
            else [self._ensure_non_token(v) for v in arg]
            for k, arg in kwargs.items()
        }
        instance = exp_class(**kwargs)
        self.validate_expression(instance)
        return instance

    def validate_expression(self, expression):
        if self.error_level == ErrorLevel.IGNORE:
            return

        for k in expression.args:
            if k not in expression.arg_types:
                self.raise_error(
                    f"Unexpected keyword: '{k}' for {expression.__class__}"
                )
        for k, mandatory in expression.arg_types.items():
            v = expression.args.get(k)
            if mandatory and (v is None or v == []):
                self.raise_error(
                    f"Required keyword: '{k}' missing for {expression.__class__}"
                )

    def _find_token(self, token, code):
        line = 1
        col = 1
        index = 0

        while line < token.line or col < token.col:
            if code[index] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            index += 1

        return index

    def _advance(self):
        self._index += 1
        self._curr = list_get(self._tokens, self._index)
        self._next = list_get(self._tokens, self._index + 1)
        self._prev = (
            list_get(self._tokens, self._index - 1) if self._index > 0 else None
        )

    def _parse_statement(self):
        if self._curr is None:
            return None

        if self._match(TokenType.CREATE):
            return self._parse_create()

        if self._match(TokenType.DROP):
            return self._parse_drop()

        if self._match(TokenType.INSERT):
            return self._parse_insert()

        if self._match(TokenType.UPDATE):
            return self._parse_update()

        cte = self._parse_cte()

        if cte:
            return cte

        return self._parse_expression()

    def _parse_drop(self):
        if self._match(TokenType.TABLE):
            kind = "table"
        elif self._match(TokenType.VIEW):
            kind = "view"
        else:
            self.raise_error("Expected TABLE or View")

        return self.expression(
            exp.Drop,
            exists=self._parse_exists(),
            this=self._parse_table(None),
            kind=kind,
        )

    def _parse_exists(self, not_=False):
        return (
            self._match(TokenType.IF)
            and (not not_ or self._match(TokenType.NOT))
            and self._match(TokenType.EXISTS)
        )

    def _parse_create(self):
        temporary = bool(self._match(TokenType.TEMPORARY))
        replace = bool(self._match(TokenType.OR) and self._match(TokenType.REPLACE))

        create_token = self._match(TokenType.TABLE, TokenType.VIEW)

        if not create_token:
            self.raise_error("Expected TABLE or View")

        exists = self._parse_exists(not_=True)
        this = self._parse_table(None, schema=True)
        expression = None
        file_format = None

        if create_token.token_type == TokenType.TABLE:
            if self._match(TokenType.STORED):
                self._match(TokenType.ALIAS)
                file_format = self.expression(exp.FileFormat, this=self._parse_id_var())
            elif self._match(TokenType.WITH):
                self._match(TokenType.L_PAREN)
                self._match(TokenType.FORMAT)
                self._match(TokenType.EQ)
                file_format = self.expression(
                    exp.FileFormat, this=self._parse_primary()
                )
                if not self._match(TokenType.R_PAREN):
                    self.raise_error("Expected ) after format")

        if self._match(TokenType.ALIAS):
            expression = self._parse_select()

        options = {
            "engine": None,
            "auto_increment": None,
            "character_set": None,
            "collate": None,
            "comment": None,
            "parsed": True,
        }

        def parse_option(option, token, option_lambda):
            if not options[option] and self._match(token):
                self._match(TokenType.EQ)
                options[option] = option_lambda()
                options["parsed"] = True

        while options["parsed"]:
            options["parsed"] = False

            parse_option("engine", TokenType.ENGINE, lambda: self._match(TokenType.VAR))
            parse_option(
                "auto_increment",
                TokenType.AUTO_INCREMENT,
                lambda: self._match(TokenType.NUMBER),
            )
            parse_option(
                "collate", TokenType.COLLATE, lambda: self._match(TokenType.VAR)
            )
            parse_option(
                "comment",
                TokenType.SCHEMA_COMMENT,
                lambda: self._match(TokenType.STRING),
            )

            if not options["character_set"]:
                default = bool(self._match(TokenType.DEFAULT))
                parse_option(
                    "character_set",
                    TokenType.CHARACTER_SET,
                    lambda: self.expression(
                        exp.CharacterSet,
                        this=self._match(TokenType.VAR),
                        default=default,
                    ),
                )

        options.pop("parsed")

        return self.expression(
            exp.Create,
            this=this,
            kind=create_token,
            expression=expression,
            exists=exists,
            file_format=file_format,
            temporary=temporary,
            replace=replace,
            **options,
        )

    def _parse_insert(self):
        overwrite = bool(self._match(TokenType.OVERWRITE))
        self._match(TokenType.INTO)
        self._match(TokenType.TABLE)

        return self.expression(
            exp.Insert,
            this=self._parse_table(None),
            exists=bool(self._parse_exists()),
            expression=self._parse_select(),
            overwrite=overwrite,
        )

    def _parse_update(self):
        return self.expression(
            exp.Update,
            this=self._parse_table(None),
            expressions=self._match(TokenType.SET)
            and self._parse_csv(self._parse_equality),
            where=self._parse_where(),
        )

    def _parse_values(self):
        if not self._match(TokenType.VALUES):
            return None

        return self.expression(
            exp.Values, expressions=self._parse_csv(self._parse_value)
        )

    def _parse_value(self):
        if not self._match(TokenType.L_PAREN):
            self.raise_error("Expected ( for values")
        expressions = self._parse_csv(self._parse_conjunction)
        if not self._match(TokenType.R_PAREN):
            self.raise_error("Expected ) for values")
        return self.expression(exp.Tuple, expressions=expressions)

    def _parse_cte(self):
        if not self._match(TokenType.WITH):
            return self._parse_select()

        expressions = []

        while True:
            recursive = self._match(TokenType.RECURSIVE)
            alias = self._parse_function(
                self._match(TokenType.IDENTIFIER, TokenType.VAR)
            )

            if not alias:
                self.raise_error("Expected alias after WITH")

            if not self._match(TokenType.ALIAS):
                self.raise_error("Expected AS after WITH")

            expressions.append(self._parse_table(alias=alias))

            if not self._match(TokenType.COMMA):
                break

        return self.expression(
            exp.CTE,
            this=self._parse_select(),
            expressions=expressions,
            recursive=bool(recursive),
        )

    def _parse_select(self):
        if self._match(TokenType.SELECT):
            this = self.expression(
                exp.Select,
                hint=self._parse_hint(),
                distinct=bool(self._match(TokenType.DISTINCT)),
                expressions=self._parse_csv(self._parse_expression),
                **{
                    "from": self._parse_from(),
                    "laterals": self._parse_laterals(),
                    "joins": self._parse_joins(),
                    "where": self._parse_where(),
                    "group": self._parse_group(),
                    "having": self._parse_having(),
                    "order": self._parse_order(),
                    "limit": self._parse_limit(),
                },
            )
        else:
            this = self._parse_values()

        return self._parse_union(this)

    def _parse_hint(self):
        if self._match(TokenType.HINT):
            hint = self._parse_primary()
            if not self._match(TokenType.COMMENT_END):
                self.raise_error("Expected */ after HINT")
            return self.expression(exp.Hint, this=hint)
        return None

    def _parse_from(self):
        if not self._match(TokenType.FROM):
            return None

        return self.expression(exp.From, expressions=self._parse_csv(self._parse_table))

    def _parse_laterals(self):
        laterals = []

        while True:
            if not self._match(TokenType.LATERAL):
                return laterals

            if not self._match(TokenType.VIEW):
                self.raise_error("Expected VIEW afteral LATERAL")

            outer = bool(self._match(TokenType.OUTER))
            this = self._parse_primary()
            table = self._parse_id_var()

            if self._match(TokenType.ALIAS):
                columns = self._parse_csv(self._parse_id_var)

            laterals.append(
                self.expression(
                    exp.Lateral,
                    this=this,
                    outer=outer,
                    table=self.expression(exp.Table, this=table),
                    columns=columns,
                )
            )

    def _parse_joins(self):
        joins = []

        while True:
            side = self._match(TokenType.LEFT, TokenType.RIGHT, TokenType.FULL)
            kind = self._match(TokenType.INNER, TokenType.OUTER, TokenType.CROSS)

            if not self._match(TokenType.JOIN):
                return joins

            joins.append(
                self.expression(
                    exp.Join,
                    this=self._parse_table(),
                    side=side.text if side else None,
                    kind=kind.text if kind else None,
                    on=self._parse_conjunction() if self._match(TokenType.ON) else None,
                )
            )

    def _parse_table(self, alias=False, schema=False):
        unnest = self._parse_unnest()

        if unnest:
            return unnest

        if self._match(TokenType.L_PAREN):
            expression = self._parse_cte()

            if not self._match(TokenType.R_PAREN):
                self.raise_error("Expecting )")
        else:
            db = None
            table = self._parse_function(
                self._match(TokenType.VAR, TokenType.IDENTIFIER), schema=schema
            )

            if self._match(TokenType.DOT):
                db = table
                if not self._match(TokenType.VAR, TokenType.IDENTIFIER):
                    self.raise_error("Expected table name")
                table = self._prev

            expression = self.expression(exp.Table, this=table, db=db)

        if alias is None:
            this = expression
        elif alias:
            this = self.expression(exp.Alias, this=expression, alias=alias)
        else:
            this = self._parse_alias(expression)

        if not isinstance(this, (exp.Alias, exp.Table)):
            this = self.expression(exp.Alias, this=this, alias=None)

        return this

    def _parse_unnest(self):
        if not self._match(TokenType.UNNEST):
            return None

        if not self._match(TokenType.L_PAREN):
            self.raise_error("Expecting ( after unnest")

        expressions = self._parse_csv(self._parse_id_var)

        if not self._match(TokenType.R_PAREN):
            self.raise_error("Expecting )")

        ordinality = self._match(TokenType.WITH) and self._match(TokenType.ORDINALITY)
        self._match(TokenType.ALIAS)
        table = self._parse_id_var()

        if not self._match(TokenType.L_PAREN):
            return self.expression(
                exp.Unnest, expressions=expressions, ordinality=ordinality, table=table
            )

        columns = self._parse_csv(self._parse_id_var)
        unnest = self.expression(
            exp.Unnest,
            expressions=expressions,
            ordinality=bool(ordinality),
            table=table,
            columns=columns,
        )

        if not self._match(TokenType.R_PAREN):
            self.raise_error("Expecting )")

        return unnest

    def _parse_where(self):
        if not self._match(TokenType.WHERE):
            return None
        return self.expression(exp.Where, this=self._parse_conjunction())

    def _parse_group(self):
        if not self._match(TokenType.GROUP):
            return None

        return self.expression(
            exp.Group, expressions=self._parse_csv(self._parse_conjunction)
        )

    def _parse_having(self):
        if not self._match(TokenType.HAVING):
            return None
        return self.expression(exp.Having, this=self._parse_conjunction())

    def _parse_order(self):
        if not self._match(TokenType.ORDER):
            return None

        return self.expression(
            exp.Order, expressions=self._parse_csv(self._parse_ordered)
        )

    def _parse_ordered(self):
        this = self._parse_bitwise()
        desc = self._match(TokenType.ASC) or self._match(TokenType.DESC)
        return self.expression(
            exp.Ordered,
            this=this,
            desc=desc.token_type is TokenType.DESC if desc else False,
        )

    def _parse_limit(self):
        if not self._match(TokenType.LIMIT):
            return None

        limit_number = self._match(TokenType.NUMBER)
        return self.expression(
            exp.Limit, this=limit_number.text if limit_number else None
        )

    def _parse_union(self, this):
        if not self._match(TokenType.UNION):
            return this

        distinct = not self._match(TokenType.ALL)

        return self.expression(
            exp.Union, this=this, expression=self._parse_select(), distinct=distinct
        )

    def _parse_expression(self):
        return self._parse_alias(self._parse_conjunction())

    def _parse_conjunction(self):
        return self._parse_tokens(self._parse_equality, self.CONJUNCTION)

    def _parse_equality(self):
        return self._parse_tokens(self._parse_comparison, self.EQUALITY)

    def _parse_comparison(self):
        return self._parse_tokens(self._parse_range, self.COMPARISON)

    def _parse_range(self):
        this = self._parse_bitwise()

        negate = self._match(TokenType.NOT)

        if self._match(TokenType.LIKE):
            this = self.expression(exp.Like, this=this, expression=self._parse_term())
        elif self._match(TokenType.RLIKE):
            this = self.expression(
                exp.RegexLike, this=this, expression=self._parse_term()
            )
        elif self._match(TokenType.IN):
            if not self._match(TokenType.L_PAREN):
                self.raise_error("Expected ( after IN", self._prev)

            query = self._parse_select()

            if query:
                this = self.expression(exp.In, this=this, query=query)
            else:
                this = self.expression(
                    exp.In, this=this, expressions=self._parse_csv(self._parse_term)
                )

            if not self._match(TokenType.R_PAREN):
                self.raise_error("Expected ) after IN")
        elif self._match(TokenType.BETWEEN):
            low = self._parse_term()
            self._match(TokenType.AND)
            high = self._parse_term()
            this = self.expression(exp.Between, this=this, low=low, high=high)

        if negate:
            this = self.expression(exp.Not, this=this)

        return this

    def _parse_bitwise(self):
        return self._parse_tokens(self._parse_term, self.BITWISE)

    def _parse_term(self):
        return self._parse_tokens(self._parse_factor, self.TERM)

    def _parse_factor(self):
        return self._parse_tokens(self._parse_unary, self.FACTOR)

    def _parse_unary(self):
        if self._match(TokenType.NOT):
            return self.expression(exp.Not, this=self._parse_unary())
        if self._match(TokenType.TILDA):
            return self.expression(exp.BitwiseNot, this=self._parse_unary())
        if self._match(TokenType.DASH):
            return self.expression(exp.Neg, this=self._parse_unary())
        return self._parse_type()

    def _parse_type(self):
        if self._match(TokenType.INTERVAL):
            return self.expression(
                exp.Interval,
                this=self._match(TokenType.STRING, TokenType.NUMBER),
                unit=self._match(TokenType.VAR),
            )

        type_token = self._parse_types()
        this = self._parse_primary()

        if type_token:
            if this:
                return self.expression(exp.Cast, this=this, to=type_token)
            return type_token

        if self._match(TokenType.DCOLON):
            type_token = self._parse_types()
            if not type_token:
                self.raise_error("Expected type")
            return self.expression(exp.Cast, this=this, to=type_token)

        return self._parse_column_def(this)

    def _parse_types(self):
        if (
            self._curr
            and self._curr.token_type in self.AMBIGUOUS_TOKEN_TYPES
            and self._next
            and self._next.token_type in (TokenType.L_PAREN, TokenType.L_BRACKET)
        ):
            return None

        if self._match(TokenType.TIMESTAMP, TokenType.TIMESTAMPTZ):
            tz = self._match(TokenType.WITH)
            self._match(TokenType.WITHOUT)
            self._match(TokenType.TIME)
            self._match(TokenType.ZONE)
            if tz:
                return Token(TokenType.TIMESTAMPTZ, "TIMESTAMPTZ")
            return Token(TokenType.TIMESTAMP, "TIMESTAMP")

        return self._parse_function(self._match(*self.TYPE_TOKENS))

    def _parse_column_def(self, this):
        kind = self._parse_types()

        if not kind:
            return this

        options = {
            "not_null": None,
            "auto_increment": None,
            "collate": None,
            "comment": None,
            "default": None,
            "parsed": True,
        }

        def parse_option(option, option_lambda):
            if not options[option]:
                options[option] = option_lambda()

                if options[option]:
                    options["parsed"] = True

        while options["parsed"]:
            options["parsed"] = False
            parse_option(
                "auto_increment", lambda: bool(self._match(TokenType.AUTO_INCREMENT))
            )
            parse_option(
                "collate",
                lambda: self._match(TokenType.COLLATE) and self._match(TokenType.VAR),
            )
            parse_option(
                "default",
                lambda: self._match(TokenType.DEFAULT)
                and self._match(*self.PRIMARY_TOKENS),
            )
            parse_option(
                "not_null",
                lambda: bool(
                    self._match(TokenType.NOT) and self._match(TokenType.NULL)
                ),
            )
            parse_option(
                "comment",
                lambda: self._match(TokenType.SCHEMA_COMMENT)
                and self._match(TokenType.STRING),
            )

        options.pop("parsed")
        return self.expression(exp.ColumnDef, this=this, kind=kind, **options)

    def _parse_primary(self):
        if self._match(*self.PRIMARY_TOKENS):
            return self._prev

        if self._match(TokenType.L_PAREN):
            paren = self._prev
            this = self._parse_select() or self._parse_conjunction()

            if not self._match(TokenType.R_PAREN):
                self.raise_error("Expecting )", paren)
            return self.expression(exp.Paren, this=this)

        if self._curr is None:
            return self.raise_error("Expecting expression")

        return self._parse_column()

    def _parse_column(self):
        if self._curr.token_type in self.NON_COLUMN_TOKENS:
            return None

        self._advance()

        this = self._parse_function(self._prev)
        table = None
        db = None
        fields = None

        while self._match(TokenType.DOT):
            if db:
                fields = fields if fields else [db, table, this]
                fields.append(self._match(*self.COLUMN_TOKENS))
                continue
            if table:
                db = table
            table = this
            this = self._match(*self.COLUMN_TOKENS)

        if isinstance(this, Token) and this.token_type in self.COLUMN_TOKENS:
            if fields:
                this, table, db = None, None, None
            this = self.expression(
                exp.Column, this=this, db=db, table=table, fields=fields
            )

        return self._parse_brackets(this)

    def _parse_function(self, this, schema=False):
        if not this:
            return this
        if this.token_type == TokenType.CASE:
            return self._parse_case()
        if not self._match(TokenType.L_PAREN):
            return this

        if this.token_type == TokenType.CAST:
            this = self._parse_cast()
        elif this.token_type == TokenType.COUNT:
            this = self._parse_count()
        elif this.token_type == TokenType.EXTRACT:
            this = self._parse_extract()
        else:
            args = self._parse_csv(self._parse_conjunction)
            function = self.functions.get(this.text.upper())

            if schema:
                this = self.expression(exp.Schema, this=this, expressions=args)
            elif not callable(function):
                this = self.expression(exp.Anonymous, this=this.text, expressions=args)
            else:
                args = [self._ensure_non_token(a) for a in args]
                this = function(args)
                self.validate_expression(this)
                if len(args) > len(this.arg_types) and not this.is_var_len_args:
                    self.raise_error(
                        f"The number of provided arguments ({len(args)}) is greater than "
                        f"the maximum number of supported arguments ({len(this.arg_types)})"
                    )

        if not self._match(TokenType.R_PAREN):
            self.raise_error("Expected )")

        return self._parse_window(this)

    def _parse_case(self):
        ifs = []
        default = None

        expression = self._parse_conjunction()

        while self._match(TokenType.WHEN):
            this = self._parse_conjunction()
            self._match(TokenType.THEN)
            then = self._parse_conjunction()
            ifs.append(self.expression(exp.If, this=this, true=then))

        if self._match(TokenType.ELSE):
            default = self._parse_conjunction()

        if not self._match(TokenType.END):
            self.raise_error("Expected END after CASE", self._prev)

        return self._parse_brackets(
            self.expression(exp.Case, this=expression, ifs=ifs, default=default)
        )

    def _parse_count(self):
        return self.expression(
            exp.Count,
            distinct=self._match(TokenType.DISTINCT),
            this=self._parse_conjunction(),
        )

    def _parse_extract(self):
        this = self._match(TokenType.VAR)

        if not self._match(TokenType.FROM):
            self.raise_error("Expected FROM after EXTRACT", self._prev)

        return self.expression(exp.Extract, this=this, expression=self._parse_type())

    def _parse_cast(self):
        this = self._parse_conjunction()

        if not self._match(TokenType.ALIAS):
            self.raise_error("Expected AS after CAST")

        if not self._match(*self.TYPE_TOKENS):
            self.raise_error("Expected TYPE after CAST")

        return self.expression(
            exp.Cast,
            this=this,
            to=self._parse_function(self._parse_brackets(self._prev)),
        )

    def _parse_window(self, this):
        if not self._match(TokenType.OVER):
            return this

        if not self._match(TokenType.L_PAREN):
            self.raise_error("Expecting ( after OVER")

        partition = None

        if self._match(TokenType.PARTITION):
            partition = self._parse_csv(self._parse_type)

        order = self._parse_order()

        spec = None
        kind = self._match(TokenType.ROWS, TokenType.RANGE)

        if kind:
            self._match(TokenType.BETWEEN)
            start = self._parse_window_spec()
            self._match(TokenType.AND)
            end = self._parse_window_spec()

            spec = self.expression(
                exp.WindowSpec,
                kind=kind,
                start=start["value"],
                start_side=start["side"],
                end=end["value"],
                end_side=end["side"],
            )

        if not self._match(TokenType.R_PAREN):
            self.raise_error("Expecting )")

        return self.expression(
            exp.Window, this=this, partition=partition, order=order, spec=spec
        )

    def _parse_window_spec(self):
        self._match(TokenType.BETWEEN)

        return {
            "value": self._match(TokenType.UNBOUNDED, TokenType.CURRENT_ROW)
            or self._parse_bitwise(),
            "side": self._match(TokenType.PRECEDING, TokenType.FOLLOWING),
        }

    def _parse_brackets(self, this):
        if not self._match(TokenType.L_BRACKET):
            return this

        expressions = self._parse_csv(self._parse_conjunction)

        if isinstance(this, Token) and this.token_type == TokenType.ARRAY:
            bracket = self.expression(exp.Array, expressions=expressions)
        else:
            bracket = self.expression(exp.Bracket, this=this, expressions=expressions)

        if not self._match(TokenType.R_BRACKET):
            self.raise_error("Expected ]")

        return self._parse_brackets(self._parse_dot(bracket))

    def _parse_dot(self, this):
        while self._match(TokenType.DOT):
            this = self.expression(exp.Dot, this=this, expression=self._parse_id_var())
        return this

    def _parse_alias(self, this):
        self._match(TokenType.ALIAS)

        alias = self._parse_id_var()
        if alias:
            return self.expression(exp.Alias, this=this, alias=alias)

        return this

    def _parse_id_var(self):
        return self._match(*self.ID_VAR_TOKENS)

    def _parse_csv(self, parse):
        parse_result = parse()
        items = [parse_result] if parse_result is not None else []

        while self._match(TokenType.COMMA):
            parse_result = parse()
            if parse_result is not None:
                items.append(parse_result)

        return items

    def _parse_tokens(self, parse, expressions):
        this = parse()

        while self._match(*expressions):
            this = self.expression(
                expressions[self._prev.token_type], this=this, expression=parse()
            )

        return this

    def _ensure_non_token(self, value):
        if value is None or not isinstance(value, Token):
            return value

        transformer = self.TOKEN_TO_EXPRESSION.get(value.token_type)
        if transformer:
            return transformer(value)
        return value.text

    def _match(self, *types):
        if not self._curr:
            return None

        for token_type in types:
            if self._curr.token_type == token_type:
                self._advance()
                return self._prev

        return None
