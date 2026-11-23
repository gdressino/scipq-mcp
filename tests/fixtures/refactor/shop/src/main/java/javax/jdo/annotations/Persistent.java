package javax.jdo.annotations;

public @interface Persistent {
    String name() default "";

    String mappedBy() default "";

    String column() default "";

    String table() default "";
}
