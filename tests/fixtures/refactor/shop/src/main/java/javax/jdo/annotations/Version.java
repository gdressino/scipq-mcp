package javax.jdo.annotations;

public @interface Version {
    VersionStrategy strategy() default VersionStrategy.NONE;

    String column() default "";

    Extension[] extensions() default {};
}
