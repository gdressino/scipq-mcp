// scipq-mcp refactor worker: JavaParser used as a syntax oracle.
//
// It reads one JSON request per line on stdin and writes one JSON response per line on stdout.
// It never opens a file: sources arrive in the requests. scipq-mcp starts it for one tool call
// and closes its stdin at the end. Requests ("op"):
//   hello     the JavaParser version and the language levels it knows
//   parse     does a text (a template) parse as an expression, statement, member…
//   analyze   for positions in files (SCIP occurrences): the node there and its parts, the
//             names declared around it, the types enclosing it; file-level facts
//   validate  does an edited file parse, and is there a node of the expected kind exactly over
//             each edited region; its comments
// Positions are JavaParser's: 1-based lines and columns, columns in UTF-16 units, a tab is one
// column, ends inclusive.
//
// Part of scipq-mcp (MIT License).

import com.github.javaparser.JavaParser;
import com.github.javaparser.JavaToken;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.Problem;
import com.github.javaparser.Range;
import com.github.javaparser.TokenRange;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.PackageDeclaration;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.CallableDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.InitializerDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.comments.Comment;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.LambdaExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.MethodReferenceExpr;
import com.github.javaparser.ast.expr.Name;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.SimpleName;
import com.github.javaparser.ast.expr.TypePatternExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithSimpleName;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.stmt.Statement;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.metamodel.PropertyMetaModel;

import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Properties;
import java.util.Set;

public final class ScipqWorker {

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in,
                StandardCharsets.UTF_8));
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true,
                "UTF-8");
        String line;
        while ((line = in.readLine()) != null) {
            if (line.trim().isEmpty()) {
                continue;
            }
            Object response;
            try {
                response = handle(asMap(Json.parse(line)));
            } catch (Throwable t) {
                response = map("error", String.valueOf(t));
            }
            StringBuilder b = new StringBuilder();
            Json.write(b, response);
            out.println(b);
            out.flush();
        }
    }

    static Object handle(Map<String, Object> req) {
        String op = str(req.get("op"));
        switch (op) {
            case "hello":
                return hello();
            case "parse":
                return parseOp(req);
            case "analyze":
                return analyze(req);
            case "validate":
                return validate(req);
            default:
                return map("error", "unknown op " + op);
        }
    }

    // --- hello -----------------------------------------------------------------------------

    static Object hello() {
        String version = "";
        try (InputStream s = JavaParser.class.getResourceAsStream(
                "/META-INF/maven/com.github.javaparser/javaparser-core/pom.properties")) {
            if (s != null) {
                Properties p = new Properties();
                p.load(s);
                version = p.getProperty("version", "");
            }
        } catch (Exception e) {
            // unknown version
        }
        if (version.isEmpty() && JavaParser.class.getPackage().getImplementationVersion() != null) {
            version = JavaParser.class.getPackage().getImplementationVersion();
        }
        List<Object> levels = new ArrayList<>();
        for (ParserConfiguration.LanguageLevel l : ParserConfiguration.LanguageLevel.values()) {
            levels.add(l.name());
        }
        return map("version", version, "levels", levels,
                "java", System.getProperty("java.version"));
    }

    static JavaParser parser(Object level) {
        ParserConfiguration c = new ParserConfiguration();
        c.setLanguageLevel(ParserConfiguration.LanguageLevel.valueOf(str(level)));
        c.setTabSize(1);
        c.setStoreTokens(true);
        c.setAttributeComments(true);
        return new JavaParser(c);
    }

    static List<Object> problems(ParseResult<?> r) {
        List<Object> out = new ArrayList<>();
        for (Problem p : r.getProblems()) {
            String msg = p.getMessage();
            int cut = msg.indexOf("\n\nWas expecting");
            if (cut >= 0) {
                msg = msg.substring(0, cut);
            }
            msg = msg.replaceAll("\\s+", " ").trim();
            Map<String, Object> m = map("message", msg.length() > 300 ? msg.substring(0, 300)
                    + "…" : msg);
            p.getLocation().flatMap(TokenRange::toRange).ifPresent(rg -> m.put("at", pos(rg)));
            out.add(m);
            if (out.size() == 5) {
                break;
            }
        }
        return out;
    }

    // --- parse -----------------------------------------------------------------------------

    static Object parseOp(Map<String, Object> req) {
        JavaParser p = parser(req.get("level"));
        String kind = str(req.get("kind"));
        String text = str(req.get("text"));
        ParseResult<? extends Node> r = parseAs(p, kind, text);
        if (r == null) {
            return map("error", "unknown kind " + kind);
        }
        boolean ok = r.isSuccessful() && r.getResult().isPresent();
        Map<String, Object> out = map("ok", ok, "problems", problems(r));
        if (ok) {
            out.put("node", r.getResult().get().getClass().getSimpleName());
        }
        return out;
    }

    static ParseResult<? extends Node> parseAs(JavaParser p, String kind, String text) {
        switch (kind) {
            case "expression":
                return p.parseExpression(text);
            case "statement":
                return p.parseStatement(text);
            case "block":
                return p.parseBlock(text);
            case "member":
                return p.parseBodyDeclaration(text);
            case "type":
                return p.parseType(text);
            case "annotation":
                return p.parseAnnotation(text);
            case "import":
                return p.parseImport(text);
            case "parameter":
                return p.parseParameter(text);
            case "name":
                return p.parseName(text);
            case "compilation_unit":
                return p.parse(text);
            default:
                return null;
        }
    }

    // --- analyze ---------------------------------------------------------------------------

    /** An identifier in the tree: a SimpleName, the last segment of a Name, a method
     *  reference's identifier. */
    static final class Ident {
        final Node node;
        final String id;
        final int line;
        final int col;
        final int endCol;

        Ident(Node node, String id, int line, int col, int endCol) {
            this.node = node;
            this.id = id;
            this.line = line;
            this.col = col;
            this.endCol = endCol;
        }
    }

    static List<Ident> idents(Node root) {
        List<Ident> out = new ArrayList<>();
        root.walk(n -> {
            Optional<Range> r = n.getRange();
            if (!r.isPresent()) {
                return;
            }
            Range g = r.get();
            if (n instanceof SimpleName) {
                out.add(new Ident(n, ((SimpleName) n).getIdentifier(), g.begin.line,
                        g.begin.column, g.end.column));
            } else if (n instanceof Name) {
                String id = ((Name) n).getIdentifier();
                out.add(new Ident(n, id, g.end.line, g.end.column - id.length() + 1,
                        g.end.column));
            } else if (n instanceof MethodReferenceExpr) {
                String id = ((MethodReferenceExpr) n).getIdentifier();
                out.add(new Ident(n, id, g.end.line, g.end.column - id.length() + 1,
                        g.end.column));
            }
        });
        return out;
    }

    static Object analyze(Map<String, Object> req) {
        JavaParser p = parser(req.get("level"));
        List<Object> files = new ArrayList<>();
        for (Object f : asList(req.get("files"))) {
            Map<String, Object> file = asMap(f);
            Map<String, Object> res = map("path", file.get("path"));
            try {
                analyzeFile(p, file, res);
            } catch (Throwable t) {
                res.put("error", String.valueOf(t));
            }
            files.add(res);
        }
        return map("files", files);
    }

    static void analyzeFile(JavaParser p, Map<String, Object> file, Map<String, Object> res) {
        String text = str(file.get("text"));
        ParseResult<CompilationUnit> r = p.parse(text);
        if (!r.isSuccessful() || !r.getResult().isPresent()) {
            res.put("problems", problems(r));
            return;
        }
        CompilationUnit cu = r.getResult().get();
        res.put("unit", unitFacts(cu));
        List<Ident> ids = idents(cu);
        List<Object> sites = new ArrayList<>();
        for (Object s : asList(file.get("sites"))) {
            Map<String, Object> site = asMap(s);
            Map<String, Object> out = map("id", site.get("id"));
            try {
                Ident at = locate(ids, site, text);
                Node n = select(at.node, str(site.getOrDefault("node", "name")));
                out.put("found", true);
                out.put("ident", list(at.line, at.col, at.line, at.endCol));
                out.put("node", info(n, true));
                context(at.node, out);
                Object where = site.get("where");
                if (where != null) {
                    whereMatch(p, asMap(where), n, out);
                }
            } catch (Fail e) {
                out.put("found", false);
                out.put("reason", e.getMessage());
            }
            sites.add(out);
        }
        res.put("sites", sites);
        List<Object> scopes = new ArrayList<>();
        for (Object s : asList(file.get("scopes"))) {
            Map<String, Object> scope = asMap(s);
            Map<String, Object> out = map("id", scope.get("id"));
            try {
                Ident at = locate(ids, scope, text);
                Node decl = at.node.getParentNode().orElseThrow(() -> new Fail("no parent"));
                Class<? extends Node> kind = nodeClass(str(scope.get("kind")));
                List<Object> found = new ArrayList<>();
                Object where = scope.get("where");
                for (Node n : decl.findAll(kind)) {
                    Map<String, Object> one = map("node", info(n, true));
                    Node anchor = n instanceof NodeWithSimpleName
                            ? ((NodeWithSimpleName<?>) n).getName() : n;
                    context(anchor, one);
                    if (where != null) {
                        whereMatch(p, asMap(where), n, one);
                        if (!Boolean.TRUE.equals(one.get("match"))) {
                            continue;
                        }
                    }
                    found.add(one);
                    if (found.size() >= 5000) {
                        break;
                    }
                }
                out.put("found", true);
                out.put("nodes", found);
            } catch (Fail e) {
                out.put("found", false);
                out.put("reason", e.getMessage());
            }
            scopes.add(out);
        }
        res.put("scopes", scopes);
    }

    static Map<String, Object> unitFacts(CompilationUnit cu) {
        Map<String, Object> m = new LinkedHashMap<>();
        cu.getPackageDeclaration().ifPresent(pd -> m.put("package", map(
                "name", pd.getNameAsString(), "range", range(pd),
                "name_range", range(pd.getName()))));
        List<Object> imports = new ArrayList<>();
        for (ImportDeclaration i : cu.getImports()) {
            imports.add(map("name", i.getNameAsString(), "static", i.isStatic(),
                    "asterisk", i.isAsterisk(), "range", range(i)));
        }
        m.put("imports", imports);
        List<Object> types = new ArrayList<>();
        for (TypeDeclaration<?> t : cu.getTypes()) {
            types.add(map("name", t.getNameAsString(), "range", range(t),
                    "name_range", range(t.getName()), "public", t.isPublic()));
        }
        m.put("types", types);
        m.put("comments", comments(cu));
        return m;
    }

    static List<Object> comments(CompilationUnit cu) {
        List<Object> out = new ArrayList<>();
        for (Comment c : cu.getAllComments()) {
            out.add(map("range", range(c), "text", c.getContent()));
        }
        return out;
    }

    static final class Fail extends RuntimeException {
        Fail(String m) {
            super(m);
        }
    }

    /** The identifier at a SCIP position (0-based line and column): exactly there, else the
     *  only one with that name on the line, else the one whose column matches with tabs
     *  expanded to 8 (javac's way of counting). */
    static Ident locate(List<Ident> ids, Map<String, Object> site, String text) {
        int line = num(site.get("line")) + 1;
        int col = num(site.get("col")) + 1;
        String name = str(site.getOrDefault("name", ""));
        List<Ident> exact = new ArrayList<>();
        List<Ident> sameName = new ArrayList<>();
        for (Ident i : ids) {
            if (i.line != line) {
                continue;
            }
            if (i.col == col) {
                exact.add(i);
            }
            if (i.id.equals(name)) {
                sameName.add(i);
            }
        }
        for (Ident i : exact) {
            if (name.isEmpty() || i.id.equals(name)) {
                return i;
            }
        }
        if (sameName.size() == 1) {
            return sameName.get(0);
        }
        if (sameName.size() > 1) {
            String lineText = lineOf(text, line);
            for (Ident i : sameName) {
                if (expanded(lineText, i.col) == col) {
                    return i;
                }
            }
            throw new Fail("several `" + name + "` on line " + line
                    + " and none at column " + col);
        }
        throw new Fail(exact.isEmpty() ? "no `" + name + "` on line " + line
                : "`" + exact.get(0).id + "` at line " + line + ":" + col + ", not `" + name
                        + "`");
    }

    static String lineOf(String text, int line) {
        int start = 0;
        for (int k = 1; k < line; k++) {
            int nl = text.indexOf('\n', start);
            if (nl < 0) {
                return "";
            }
            start = nl + 1;
        }
        int end = text.indexOf('\n', start);
        return text.substring(start, end < 0 ? text.length() : end);
    }

    static int expanded(String line, int col) {
        int c = 1;
        for (int k = 0; k < col - 1 && k < line.length(); k++) {
            c = line.charAt(k) == '\t' ? ((c - 1) / 8 + 1) * 8 + 1 : c + 1;
        }
        return c;
    }

    static Node parent(Node n) {
        return n.getParentNode().orElse(null);
    }

    /** The node a step acts on, from the identifier at its site. */
    static Node select(Node anchor, String spec) {
        Node p = parent(anchor);
        if (spec.equals("name")) {
            return anchor;
        }
        if (spec.equals("expr")) {
            if (anchor instanceof MethodReferenceExpr) {
                return anchor;
            }
            if (p instanceof MethodCallExpr && ((MethodCallExpr) p).getName() == anchor
                    || p instanceof FieldAccessExpr && ((FieldAccessExpr) p).getName() == anchor
                    || p instanceof NameExpr
                    || p instanceof ClassOrInterfaceType
                    && ((ClassOrInterfaceType) p).getName() == anchor) {
                return p;
            }
            return anchor;
        }
        if (spec.equals("call") || spec.startsWith("argument[")) {
            Node call = null;
            if (p instanceof MethodCallExpr && ((MethodCallExpr) p).getName() == anchor) {
                call = p;
            } else if (p instanceof ClassOrInterfaceType && parent(p) instanceof ObjectCreationExpr
                    && ((ObjectCreationExpr) parent(p)).getType() == p) {
                call = parent(p);
            } else if (anchor instanceof MethodReferenceExpr) {
                throw new Fail("a method reference (" + anchor + "), not a call");
            }
            if (call == null) {
                throw new Fail("not a call: " + describe(p != null ? p : anchor));
            }
            if (spec.equals("call")) {
                return call;
            }
            NodeList<?> args = call instanceof MethodCallExpr
                    ? ((MethodCallExpr) call).getArguments()
                    : ((ObjectCreationExpr) call).getArguments();
            int k = index(spec);
            if (k >= args.size()) {
                throw new Fail(describe(call) + " has " + args.size() + " arguments, no "
                        + spec);
            }
            return args.get(k);
        }
        if (spec.equals("statement")) {
            for (Node n = p; n != null; n = parent(n)) {
                if (n instanceof Statement && !(n instanceof BlockStmt)) {
                    return n;
                }
                if (n instanceof BodyDeclaration) {
                    break;
                }
            }
            throw new Fail("not inside a statement");
        }
        if (spec.equals("declaration") || spec.startsWith("parameter[")) {
            Node decl = declarationOf(anchor);
            if (spec.equals("declaration")) {
                return decl;
            }
            NodeList<Parameter> params;
            if (decl instanceof CallableDeclaration) {
                params = ((CallableDeclaration<?>) decl).getParameters();
            } else if (decl instanceof LambdaExpr) {
                params = ((LambdaExpr) decl).getParameters();
            } else {
                throw new Fail("not a method or constructor declaration: " + describe(decl));
            }
            int k = index(spec);
            if (k >= params.size()) {
                throw new Fail(describe(decl) + " has " + params.size() + " parameters, no "
                        + spec);
            }
            return params.get(k);
        }
        if (spec.startsWith("ancestor:")) {
            Class<? extends Node> c = nodeClass(spec.substring("ancestor:".length()));
            for (Node n = p; n != null; n = parent(n)) {
                if (c.isInstance(n)) {
                    return n;
                }
            }
            throw new Fail("no enclosing " + c.getSimpleName());
        }
        throw new Fail("unknown node spec " + spec);
    }

    /** The declaration whose name is `anchor` (a field's: its FieldDeclaration). */
    static Node declarationOf(Node anchor) {
        Node p = parent(anchor);
        if (p instanceof VariableDeclarator && ((VariableDeclarator) p).getName() == anchor) {
            Node q = parent(p);
            return q instanceof FieldDeclaration ? q : p;
        }
        if (p instanceof NodeWithSimpleName && ((NodeWithSimpleName<?>) p).getName() == anchor
                && (p instanceof BodyDeclaration || p instanceof Parameter)) {
            return p;
        }
        throw new Fail("not a declaration: " + describe(p != null ? p : anchor));
    }

    static int index(String spec) {
        try {
            return Integer.parseInt(spec.substring(spec.indexOf('[') + 1, spec.indexOf(']')));
        } catch (RuntimeException e) {
            throw new Fail("bad index in " + spec);
        }
    }

    static String describe(Node n) {
        String s = n.toString().replaceAll("\\s+", " ");
        return n.getClass().getSimpleName() + " `" + (s.length() > 60 ? s.substring(0, 60) + "…"
                : s) + "`" + n.getRange().map(r -> " at line " + r.begin.line).orElse("");
    }

    static final String[] PACKAGES = {"com.github.javaparser.ast.",
        "com.github.javaparser.ast.body.", "com.github.javaparser.ast.expr.",
        "com.github.javaparser.ast.stmt.", "com.github.javaparser.ast.type.",
        "com.github.javaparser.ast.comments.", "com.github.javaparser.ast.modules."};

    @SuppressWarnings("unchecked")
    static Class<? extends Node> nodeClass(String kind) {
        String k = kind;
        if (k.contains("_") || Character.isLowerCase(k.isEmpty() ? 'A' : k.charAt(0))) {
            StringBuilder b = new StringBuilder();
            for (String part : k.split("_")) {
                if (!part.isEmpty()) {
                    b.append(Character.toUpperCase(part.charAt(0))).append(part.substring(1));
                }
            }
            k = b.toString();
        }
        for (String pkg : PACKAGES) {
            try {
                Class<?> c = Class.forName(pkg + k);
                if (Node.class.isAssignableFrom(c)) {
                    return (Class<? extends Node>) c;
                }
            } catch (ClassNotFoundException e) {
                // next package
            }
        }
        throw new Fail("unknown node kind " + kind + " (a JavaParser class: MethodCallExpr, "
                + "IfStmt, CatchClause…)");
    }

    /** A node: its kind, range, parts (JavaParser's properties: scope, name, arguments…),
     *  the comment attached to it, and the delimiters of its own lists. */
    static Map<String, Object> info(Node n, boolean deep) {
        Map<String, Object> m = map("kind", n.getClass().getSimpleName(), "range", range(n));
        n.getComment().ifPresent(c -> m.put("comment", range(c)));
        if (!deep) {
            return m;
        }
        Map<String, Object> parts = new LinkedHashMap<>();
        for (PropertyMetaModel pm : n.getMetaModel().getAllPropertyMetaModels()) {
            if (pm.getName().equals("comment")) {
                continue;
            }
            Object v;
            try {
                v = pm.getValue(n);
            } catch (RuntimeException e) {
                continue;
            }
            if (v instanceof Optional) {
                v = ((Optional<?>) v).orElse(null);
            }
            if (v instanceof Node) {
                parts.put(pm.getName(), info((Node) v, false));
            } else if (v instanceof NodeList) {
                List<Object> l = new ArrayList<>();
                for (Node x : (NodeList<?>) v) {
                    l.add(info(x, false));
                }
                parts.put(pm.getName(), l);
            } else if (v != null && pm.isAttribute()) {
                parts.put(pm.getName(), String.valueOf(v));
            }
        }
        m.put("parts", parts);
        m.put("tokens", delimiters(n));
        return m;
    }

    /** Separators and comments in a node's own tokens, up to its body (a method's, an
     *  anonymous class's): what list edits need ("(", ")", ",", "<", ">", comments). */
    static List<Object> delimiters(Node n) {
        List<Object> out = new ArrayList<>();
        Optional<TokenRange> tr = n.getTokenRange();
        if (!tr.isPresent()) {
            return out;
        }
        Range stop = null;
        if (n instanceof CallableDeclaration) {
            Node body = n instanceof com.github.javaparser.ast.body.MethodDeclaration
                    ? ((com.github.javaparser.ast.body.MethodDeclaration) n).getBody()
                            .orElse(null)
                    : ((com.github.javaparser.ast.body.ConstructorDeclaration) n).getBody();
            stop = body == null ? null : body.getRange().orElse(null);
        } else if (n instanceof ObjectCreationExpr) {
            NodeList<BodyDeclaration<?>> body = ((ObjectCreationExpr) n)
                    .getAnonymousClassBody().orElse(null);
            if (body != null && body.isNonEmpty()) {
                stop = body.get(0).getRange().orElse(null);
            }
        } else if (n instanceof TypeDeclaration || n instanceof BlockStmt
                || n instanceof Statement && !(n instanceof com.github.javaparser.ast.stmt
                .ExpressionStmt)) {
            return out;  // large: lists of a type or a block are not edited this way
        }
        int count = 0;
        for (JavaToken t : tr.get()) {
            Optional<Range> r = t.getRange();
            if (!r.isPresent()) {
                continue;
            }
            if (stop != null && !r.get().begin.isBefore(stop.begin)) {
                break;
            }
            String text = t.getText();
            boolean keep = t.getCategory().isComment() || text.equals("(") || text.equals(")")
                    || text.equals(",") || text.equals("<") || text.equals(">")
                    || text.equals(">>") || text.equals(">>>") || text.equals("{")
                    || text.equals("}");
            if (keep) {
                out.add(map("t", t.getCategory().isComment() ? "comment" : text,
                        "range", range(r.get())));
                if (++count >= 2000) {
                    break;
                }
            }
        }
        return out;
    }

    /** Around a site: the names declared in its member (locals, parameters, patterns), whether
     *  it is qualified, and the types enclosing it, innermost first. */
    static void context(Node anchor, Map<String, Object> out) {
        Node p = parent(anchor);
        boolean qualified = p instanceof MethodCallExpr
                && ((MethodCallExpr) p).getName() == anchor
                && ((MethodCallExpr) p).getScope().isPresent()
                || p instanceof FieldAccessExpr && ((FieldAccessExpr) p).getName() == anchor
                || anchor instanceof MethodReferenceExpr
                || anchor instanceof Name && ((Name) anchor).getQualifier().isPresent()
                || p instanceof ClassOrInterfaceType
                && ((ClassOrInterfaceType) p).getScope().isPresent();
        out.put("qualified", qualified);
        Node member = null;
        for (Node n = p; n != null; n = parent(n)) {
            if (n instanceof CallableDeclaration || n instanceof InitializerDeclaration
                    || n instanceof FieldDeclaration) {
                member = n;
                break;
            }
            if (n instanceof TypeDeclaration) {
                break;
            }
        }
        Set<String> locals = new LinkedHashSet<>();
        if (member != null) {
            member.walk(n -> {
                if (n instanceof Parameter) {
                    locals.add(((Parameter) n).getNameAsString());
                } else if (n instanceof VariableDeclarator) {
                    locals.add(((VariableDeclarator) n).getNameAsString());
                } else if (n instanceof TypePatternExpr) {
                    locals.add(((TypePatternExpr) n).getNameAsString());
                }
            });
            if (member instanceof FieldDeclaration) {
                for (VariableDeclarator v : ((FieldDeclaration) member).getVariables()) {
                    locals.remove(v.getNameAsString());
                }
            }
            out.put("member", info(member, false));
        }
        out.put("locals", new ArrayList<Object>(locals));
        List<Object> enclosing = new ArrayList<>();
        for (Node n = p; n != null; n = parent(n)) {
            if (n instanceof ObjectCreationExpr
                    && ((ObjectCreationExpr) n).getAnonymousClassBody().isPresent()) {
                ObjectCreationExpr oc = (ObjectCreationExpr) n;
                if (anchor == oc.getType().getName()) {
                    continue;  // the anonymous class's own supertype name
                }
                enclosing.add(map("kind", "anonymous",
                        "type", list(range(oc.getType().getName()).toArray()),
                        "members", memberNames(oc.getAnonymousClassBody().get())));
            } else if (n instanceof TypeDeclaration) {
                TypeDeclaration<?> t = (TypeDeclaration<?>) n;
                enclosing.add(map("kind", "type", "name", t.getNameAsString(),
                        "name_range", range(t.getName()), "members", memberNames(t.getMembers())));
            }
        }
        out.put("enclosing", enclosing);
    }

    static List<Object> memberNames(NodeList<BodyDeclaration<?>> members) {
        Set<String> names = new LinkedHashSet<>();
        for (BodyDeclaration<?> b : members) {
            if (b instanceof FieldDeclaration) {
                for (VariableDeclarator v : ((FieldDeclaration) b).getVariables()) {
                    names.add(v.getNameAsString());
                }
            } else if (b instanceof NodeWithSimpleName) {
                names.add(((NodeWithSimpleName<?>) b).getNameAsString());
            }
        }
        return new ArrayList<Object>(names);
    }

    // --- where: structural match with placeholders ----------------------------------------

    static void whereMatch(JavaParser p, Map<String, Object> where, Node n,
                           Map<String, Object> out) {
        String kind = str(where.getOrDefault("kind", "expression"));
        ParseResult<? extends Node> r = parseAs(p, kind, str(where.get("pattern")));
        if (r == null || !r.isSuccessful() || !r.getResult().isPresent()) {
            throw new Fail("the where pattern does not parse as " + kind + ": "
                    + (r == null ? "unknown kind" : problems(r)));
        }
        Map<String, Object> caps = new LinkedHashMap<>();
        Map<String, Node> seen = new LinkedHashMap<>();
        boolean ok = match(r.getResult().get(), n, caps, seen);
        out.put("match", ok);
        if (ok) {
            out.put("captures", caps);
        }
    }

    static String placeholder(Node pat) {
        String id = pat instanceof NameExpr ? ((NameExpr) pat).getNameAsString()
                : pat instanceof SimpleName ? ((SimpleName) pat).getIdentifier() : null;
        return id != null && id.startsWith("$") ? id : null;
    }

    static boolean match(Node pat, Node n, Map<String, Object> caps, Map<String, Node> seen) {
        String ph = placeholder(pat);
        if (ph != null && !ph.startsWith("$$")) {
            Node prev = seen.get(ph);
            if (prev != null) {
                return strip(prev).equals(strip(n));
            }
            seen.put(ph, n);
            caps.put(ph, info(n, false));
            return true;
        }
        if (pat.getClass() != n.getClass()) {
            return false;
        }
        for (PropertyMetaModel pm : pat.getMetaModel().getAllPropertyMetaModels()) {
            if (pm.getName().equals("comment")) {
                continue;
            }
            Object a = unwrap(pm.getValue(pat));
            Object b = unwrap(pm.getValue(n));
            if (a instanceof NodeList) {
                if (!(b instanceof NodeList) || !matchList((NodeList<?>) a, (NodeList<?>) b,
                        caps, seen)) {
                    return false;
                }
            } else if (a instanceof Node) {
                if (!(b instanceof Node) || !match((Node) a, (Node) b, caps, seen)) {
                    return false;
                }
            } else if (a == null ? b != null : !a.equals(b)) {
                return false;
            }
        }
        return true;
    }

    static boolean matchList(NodeList<?> a, NodeList<?> b, Map<String, Object> caps,
                             Map<String, Node> seen) {
        for (int k = 0; k < a.size(); k++) {
            String ph = placeholder(a.get(k));
            if (ph != null && ph.startsWith("$$") && k == a.size() - 1) {
                List<Object> rest = new ArrayList<>();
                for (int j = k; j < b.size(); j++) {
                    rest.add(info(b.get(j), false));
                }
                caps.put(ph, rest);
                return true;
            }
            if (k >= b.size() || !match(a.get(k), b.get(k), caps, seen)) {
                return false;
            }
        }
        return a.size() == b.size();
    }

    static Object unwrap(Object v) {
        return v instanceof Optional ? ((Optional<?>) v).orElse(null) : v;
    }

    static String strip(Node n) {
        return n.toString().replaceAll("\\s+", " ");
    }

    // --- validate --------------------------------------------------------------------------

    static Object validate(Map<String, Object> req) {
        JavaParser p = parser(req.get("level"));
        List<Object> files = new ArrayList<>();
        for (Object f : asList(req.get("files"))) {
            Map<String, Object> file = asMap(f);
            Map<String, Object> res = map("path", file.get("path"));
            try {
                validateFile(p, file, res);
            } catch (Throwable t) {
                res.put("error", String.valueOf(t));
            }
            files.add(res);
        }
        return map("files", files);
    }

    static void validateFile(JavaParser p, Map<String, Object> file, Map<String, Object> res) {
        ParseResult<CompilationUnit> r = p.parse(str(file.get("text")));
        if (!r.isSuccessful() || !r.getResult().isPresent()) {
            res.put("ok", false);
            res.put("problems", problems(r));
            return;
        }
        CompilationUnit cu = r.getResult().get();
        res.put("ok", true);
        List<Ident> ids = null;
        List<Node> all = new ArrayList<>();
        cu.walk(all::add);
        List<Object> regions = new ArrayList<>();
        for (Object o : asList(file.get("regions"))) {
            Map<String, Object> reg = asMap(o);
            List<Object> b = asList(reg.get("begin"));
            List<Object> e = asList(reg.get("end"));
            int bl = num(b.get(0));
            int bc = num(b.get(1));
            int el = num(e.get(0));
            int ec = num(e.get(1));
            String kind = str(reg.getOrDefault("kind", "Node"));
            boolean ok = false;
            List<Object> there = new ArrayList<>();
            if (kind.equals("Identifier")) {
                if (ids == null) {
                    ids = idents(cu);
                }
                for (Ident i : ids) {
                    if (i.line == bl && i.col == bc && i.endCol == ec && bl == el) {
                        ok = true;
                    }
                }
            } else {
                Class<? extends Node> c = nodeClass(kind);
                for (Node n : all) {
                    Optional<Range> rg = n.getRange();
                    if (!rg.isPresent() || rg.get().begin.line != bl
                            || rg.get().begin.column != bc) {
                        continue;
                    }
                    if (rg.get().end.line == el && rg.get().end.column == ec) {
                        there.add(n.getClass().getSimpleName());
                        if (c.isInstance(n)) {
                            ok = true;
                        }
                    }
                }
            }
            regions.add(map("id", reg.get("id"), "ok", ok, "there", there));
        }
        res.put("regions", regions);
        res.put("comments", comments(cu));
        res.put("unit", unitFacts(cu));
    }

    // --- helpers -----------------------------------------------------------------------------

    static List<Object> range(Node n) {
        return n.getRange().map(ScipqWorker::range).orElse(null);
    }

    static List<Object> range(Range r) {
        return list(r.begin.line, r.begin.column, r.end.line, r.end.column);
    }

    static List<Object> pos(Range r) {
        return list(r.begin.line, r.begin.column);
    }

    static List<Object> list(Object... xs) {
        List<Object> l = new ArrayList<>();
        for (Object x : xs) {
            l.add(x);
        }
        return l;
    }

    static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int k = 0; k + 1 < kv.length; k += 2) {
            m.put((String) kv[k], kv[k + 1]);
        }
        return m;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> asMap(Object o) {
        if (o instanceof Map) {
            return (Map<String, Object>) o;
        }
        throw new Fail("expected an object");
    }

    @SuppressWarnings("unchecked")
    static List<Object> asList(Object o) {
        if (o == null) {
            return new ArrayList<>();
        }
        if (o instanceof List) {
            return (List<Object>) o;
        }
        throw new Fail("expected a list");
    }

    static String str(Object o) {
        return o == null ? "" : o.toString();
    }

    static int num(Object o) {
        if (o instanceof Number) {
            return ((Number) o).intValue();
        }
        throw new Fail("expected a number, got " + o);
    }

    /** A small JSON reader and writer (objects, arrays, strings, numbers, booleans, null). */
    static final class Json {
        private final String s;
        private int i;

        private Json(String s) {
            this.s = s;
        }

        static Object parse(String s) {
            Json j = new Json(s);
            Object v = j.value();
            j.ws();
            if (j.i != s.length()) {
                throw new Fail("trailing characters in JSON");
            }
            return v;
        }

        private void ws() {
            while (i < s.length() && " \t\r\n".indexOf(s.charAt(i)) >= 0) {
                i++;
            }
        }

        private char next() {
            if (i >= s.length()) {
                throw new Fail("truncated JSON");
            }
            return s.charAt(i++);
        }

        private void expect(char c) {
            ws();
            if (next() != c) {
                throw new Fail("bad JSON at " + (i - 1) + ": expected " + c);
            }
        }

        private Object value() {
            ws();
            if (i >= s.length()) {
                throw new Fail("truncated JSON");
            }
            char c = s.charAt(i);
            if (c == '{') {
                return object();
            }
            if (c == '[') {
                return array();
            }
            if (c == '"') {
                return string();
            }
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            return number();
        }

        private Map<String, Object> object() {
            expect('{');
            Map<String, Object> m = new LinkedHashMap<>();
            ws();
            if (i < s.length() && s.charAt(i) == '}') {
                i++;
                return m;
            }
            while (true) {
                ws();
                String k = string();
                expect(':');
                m.put(k, value());
                ws();
                char c = next();
                if (c == '}') {
                    return m;
                }
                if (c != ',') {
                    throw new Fail("bad JSON object at " + (i - 1));
                }
            }
        }

        private List<Object> array() {
            expect('[');
            List<Object> a = new ArrayList<>();
            ws();
            if (i < s.length() && s.charAt(i) == ']') {
                i++;
                return a;
            }
            while (true) {
                a.add(value());
                ws();
                char c = next();
                if (c == ']') {
                    return a;
                }
                if (c != ',') {
                    throw new Fail("bad JSON array at " + (i - 1));
                }
            }
        }

        private String string() {
            if (next() != '"') {
                throw new Fail("bad JSON string at " + (i - 1));
            }
            StringBuilder b = new StringBuilder();
            while (true) {
                char c = next();
                if (c == '"') {
                    return b.toString();
                }
                if (c != '\\') {
                    b.append(c);
                    continue;
                }
                char e = next();
                switch (e) {
                    case 'n': b.append('\n'); break;
                    case 't': b.append('\t'); break;
                    case 'r': b.append('\r'); break;
                    case 'b': b.append('\b'); break;
                    case 'f': b.append('\f'); break;
                    case 'u':
                        b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                        i += 4;
                        break;
                    default: b.append(e);
                }
            }
        }

        private Object number() {
            int start = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) {
                i++;
            }
            String t = s.substring(start, i);
            if (t.isEmpty()) {
                throw new Fail("bad JSON value at " + start);
            }
            if (t.contains(".") || t.contains("e") || t.contains("E")) {
                return Double.parseDouble(t);
            }
            return Long.parseLong(t);
        }

        static void write(StringBuilder b, Object o) {
            if (o == null) {
                b.append("null");
            } else if (o instanceof String) {
                quote(b, (String) o);
            } else if (o instanceof Boolean || o instanceof Integer || o instanceof Long) {
                b.append(o);
            } else if (o instanceof Number) {
                b.append(((Number) o).doubleValue());
            } else if (o instanceof Map) {
                b.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : ((Map<?, ?>) o).entrySet()) {
                    if (!first) {
                        b.append(',');
                    }
                    first = false;
                    quote(b, String.valueOf(e.getKey()));
                    b.append(':');
                    write(b, e.getValue());
                }
                b.append('}');
            } else if (o instanceof Collection) {
                b.append('[');
                boolean first = true;
                for (Object x : (Collection<?>) o) {
                    if (!first) {
                        b.append(',');
                    }
                    first = false;
                    write(b, x);
                }
                b.append(']');
            } else {
                quote(b, o.toString());
            }
        }

        static void quote(StringBuilder b, String s) {
            b.append('"');
            for (int k = 0; k < s.length(); k++) {
                char c = s.charAt(k);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    default:
                        if (c < 0x20 || c > 0x7e) {
                            b.append(String.format("\\u%04x", (int) c));
                        } else {
                            b.append(c);
                        }
                }
            }
            b.append('"');
        }
    }
}
