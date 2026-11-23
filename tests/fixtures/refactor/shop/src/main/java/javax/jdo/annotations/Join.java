package javax.jdo.annotations;

public @interface Join {
    String table() default "";

    String column() default "";
}
